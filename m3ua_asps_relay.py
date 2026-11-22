"""M3UA ASP state and routing relay."""

import collections
import threading

DUPLICATE_POINT_CODE = "duplicate-point-code"
NETWORK_APPEARANCE_MISMATCH = "network-appearance-mismatch"
ROUTING_CONTEXT_MISMATCH = "routing-context-mismatch"
STALE_SEQUENCE = "stale-sequence"
DUPU_OVERRUN = "dupu-overrun"
CONGESTED = "congested"
UNSUPPORTED_SI = "unsupported-si"
QUEUE_FULL = "queue-full"
UNKNOWN_ASP = "unknown-asp"
AS_INACTIVE = "as-inactive"
UNKNOWN_AS = "unknown-as"

SEQ_MODULUS = 1 << 32
SEQ_HALF_SPACE = 1 << 31


class M3uaError(Exception):
    def __init__(self, code, message="", field=None):
        self.code = code
        self.field = field
        super().__init__(code if not message else "%s: %s" % (code, message))


class M3uaAspsRelay:
    """ASP sets isolated by Routing Context with idempotent state changes.

    Protocol-data sequence comparison treats values as unsigned 32-bit
    numbers with wrap-around.  The send queue reserves a minimum share for
    low-priority traffic.
    """

    def __init__(self, queue_limit=100, low_priority_quota=2):
        self.queue_limit = queue_limit
        self.low_priority_quota = low_priority_quota
        self._lock = threading.RLock()
        self._ases = {}
        self._asps = {}
        self._point_codes = {}
        self._high_queue = collections.deque()
        self._low_queue = collections.deque()
        self._congestion = 0

    def _require_asp(self, asp_id):
        asp = self._asps.get(asp_id)
        if asp is None:
            raise M3uaError(UNKNOWN_ASP, "ASP %r is not registered" % (asp_id,),
                            field="asp_id")
        return asp

    def _check_sequence(self, asp, kind, seq):
        last = asp["last_%s_seq" % kind]
        if last is not None and seq != last and not self.sequence_in_order(last, seq):
            raise M3uaError(STALE_SEQUENCE,
                            "sequence %s is not after %s" % (seq, last),
                            field="seq")

    def _active_asp_ids(self, as_name):
        return list(self._ases[as_name]["active_asps"])

    def add_as(self, as_name, routing_context, point_codes=(), mode="override",
               network_appearance=None, service_indicators=(3,)):
        with self._lock:
            for point_code in point_codes:
                owner = self._point_codes.get(point_code)
                if owner is not None and owner != as_name:
                    raise M3uaError(
                        DUPLICATE_POINT_CODE,
                        "point code %s already claimed by AS %r"
                        % (point_code, owner),
                        field="point_code")
            target = self._ases.get(as_name)
            if target is None:
                target = {
                    "routing_context": routing_context,
                    "point_codes": [],
                    "mode": mode,
                    "network_appearance": network_appearance,
                    "service_indicators": tuple(service_indicators),
                    "active_asps": [],
                    "asp_order": [],
                }
                self._ases[as_name] = target
            else:
                target["routing_context"] = routing_context
                target["mode"] = mode
                target["network_appearance"] = network_appearance
                target["service_indicators"] = tuple(service_indicators)
            for point_code in point_codes:
                if point_code not in target["point_codes"]:
                    target["point_codes"].append(point_code)
                self._point_codes[point_code] = as_name
            return self.route_table()[as_name]

    def add_asp(self, asp_id, as_name, network_appearance=None):
        with self._lock:
            target = self._ases.get(as_name)
            if target is None:
                raise M3uaError(UNKNOWN_AS, "AS %r is not configured" % (as_name,),
                                field="as_name")
            if (network_appearance is not None
                    and target["network_appearance"] is not None
                    and network_appearance != target["network_appearance"]):
                raise M3uaError(
                    NETWORK_APPEARANCE_MISMATCH,
                    "ASP network appearance %s differs from AS %s"
                    % (network_appearance, target["network_appearance"]),
                    field="network_appearance")
            if asp_id not in self._asps:
                self._asps[asp_id] = {
                    "as_name": as_name,
                    "network_appearance": network_appearance,
                    "state": "inactive",
                    "last_active_seq": None,
                    "last_inactive_seq": None,
                }
                target["asp_order"].append(asp_id)
            return {"asp_id": asp_id, "as_name": as_name, "active": False}

    def asp_active(self, asp_id, seq, routing_context=None):
        with self._lock:
            asp = self._require_asp(asp_id)
            target = self._ases[asp["as_name"]]
            if (routing_context is not None
                    and routing_context != target["routing_context"]):
                raise M3uaError(
                    ROUTING_CONTEXT_MISMATCH,
                    "routing context %s does not belong to AS %r"
                    % (routing_context, asp["as_name"]),
                    field="routing_context")
            self._check_sequence(asp, "active", seq)
            duplicate = asp["state"] == "active"
            if not duplicate:
                asp["state"] = "active"
                if target["mode"] == "override":
                    target["active_asps"] = [asp_id]
                elif asp_id not in target["active_asps"]:
                    target["active_asps"].append(asp_id)
            asp["last_active_seq"] = seq
            return {
                "active": True,
                "duplicate": duplicate,
                "active_asps": self._active_asp_ids(asp["as_name"]),
            }

    def asp_inactive(self, asp_id, seq):
        with self._lock:
            asp = self._require_asp(asp_id)
            target = self._ases[asp["as_name"]]
            self._check_sequence(asp, "inactive", seq)
            duplicate = asp["state"] != "active"
            if not duplicate:
                asp["state"] = "inactive"
                if asp_id in target["active_asps"]:
                    target["active_asps"].remove(asp_id)
            asp["last_inactive_seq"] = seq
            return {
                "active": False,
                "duplicate": duplicate,
                "active_asps": self._active_asp_ids(asp["as_name"]),
            }

    def active_asp(self, as_name):
        with self._lock:
            target = self._ases.get(as_name)
            if target is None or not target["active_asps"]:
                return None
            return target["active_asps"][0]

    def parse_aspm(self, data):
        with self._lock:
            level = data[0] if data else 0
            self._congestion = level
            return {"congestion": level}

    def send_data(self, as_name, payload, priority=0, si=3):
        with self._lock:
            target = self._ases.get(as_name)
            if target is None or not target["active_asps"]:
                raise M3uaError(AS_INACTIVE,
                                "AS %r has no active ASP" % (as_name,),
                                field="as_name")
            if si not in target["service_indicators"]:
                raise M3uaError(UNSUPPORTED_SI,
                                "service indicator %s is not configured" % si,
                                field="si")
            if self._congestion and priority <= 0:
                raise M3uaError(CONGESTED,
                                "congestion level %s blocks priority %s"
                                % (self._congestion, priority),
                                field="priority")
            if self.queue_size() >= self.queue_limit:
                raise M3uaError(QUEUE_FULL,
                                "send queue limit %s reached" % self.queue_limit,
                                field="queue")
            message = {
                "as_name": as_name,
                "payload": payload,
                "priority": priority,
                "si": si,
            }
            if priority > 0:
                self._high_queue.append(message)
            else:
                self._low_queue.append(message)
            return message

    def next_message(self):
        with self._lock:
            if not self._high_queue and not self._low_queue:
                return None
            low_quota = self.low_priority_quota
            low_served = 0
            while self._high_queue or self._low_queue:
                if self._low_queue and low_served < low_quota:
                    low_served += 1
                    return self._low_queue.popleft()
                if self._high_queue:
                    return self._high_queue.popleft()
                if self._low_queue:
                    return self._low_queue.popleft()
            return None

    def queue_size(self):
        with self._lock:
            return len(self._high_queue) + len(self._low_queue)

    def heartbeat_timeout(self, asp_id):
        with self._lock:
            asp = self._require_asp(asp_id)
            target = self._ases[asp["as_name"]]
            failed_over = None
            if asp_id in target["active_asps"]:
                target["active_asps"].remove(asp_id)
                asp["state"] = "inactive"
                for candidate in target["asp_order"]:
                    if candidate == asp_id:
                        continue
                    backup = self._asps[candidate]
                    backup["state"] = "active"
                    if candidate not in target["active_asps"]:
                        target["active_asps"].append(candidate)
                    failed_over = candidate
                    break
            else:
                asp["state"] = "inactive"
            return {"failed_over": failed_over}

    def handle_dupu(self, data):
        with self._lock:
            if len(data) < 2:
                raise M3uaError(DUPU_OVERRUN, "DUPU parameter is truncated",
                                field="length")
            length = (data[0] << 8) | data[1]
            if length > len(data):
                raise M3uaError(
                    DUPU_OVERRUN,
                    "DUPU length %s exceeds available %s bytes"
                    % (length, len(data)),
                    field="length")
            return {"length": length}

    def sequence_in_order(self, previous, current):
        delta = (current - previous) % SEQ_MODULUS
        return 0 < delta < SEQ_HALF_SPACE

    def route_table(self):
        with self._lock:
            return {
                name: {
                    "routing_context": info["routing_context"],
                    "point_codes": list(info["point_codes"]),
                    "mode": info["mode"],
                    "network_appearance": info["network_appearance"],
                    "service_indicators": tuple(info["service_indicators"]),
                    "active_asps": list(info["active_asps"]),
                }
                for name, info in self._ases.items()
            }

    def snapshot(self):
        with self._lock:
            return {
                "ases": {
                    name: {
                        "routing_context": info["routing_context"],
                        "point_codes": list(info["point_codes"]),
                        "mode": info["mode"],
                        "network_appearance": info["network_appearance"],
                        "service_indicators": list(info["service_indicators"]),
                    }
                    for name, info in self._ases.items()
                },
                "asps": {
                    asp_id: {
                        "as_name": asp["as_name"],
                        "network_appearance": asp["network_appearance"],
                        "state": asp["state"],
                        "last_active_seq": asp["last_active_seq"],
                        "last_inactive_seq": asp["last_inactive_seq"],
                    }
                    for asp_id, asp in self._asps.items()
                },
                "congestion": self._congestion,
            }

    def restore(self, snapshot):
        with self._lock:
            self._ases = {}
            self._asps = {}
            self._point_codes = {}
            self._high_queue.clear()
            self._low_queue.clear()
            self._congestion = snapshot.get("congestion", 0)
            for name, info in snapshot.get("ases", {}).items():
                self.add_as(
                    name,
                    routing_context=info["routing_context"],
                    point_codes=info.get("point_codes", ()),
                    mode=info.get("mode", "override"),
                    network_appearance=info.get("network_appearance"),
                    service_indicators=tuple(info.get("service_indicators", (3,))),
                )
            for asp_id, asp in snapshot.get("asps", {}).items():
                self.add_asp(asp_id, asp["as_name"],
                             network_appearance=asp.get("network_appearance"))
            for asp_id, asp in snapshot.get("asps", {}).items():
                stored = self._asps[asp_id]
                stored["last_active_seq"] = asp.get("last_active_seq")
                stored["last_inactive_seq"] = asp.get("last_inactive_seq")
                if asp.get("state") == "active":
                    stored["state"] = "active"
                    active_set = self._ases[stored["as_name"]]["active_asps"]
                    if asp_id not in active_set:
                        active_set.append(asp_id)
            return self.snapshot()
