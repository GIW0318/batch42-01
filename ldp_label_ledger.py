"""LDP FEC/label ledger."""

import hashlib
import heapq
import hmac

AUTH_FAILED = "auth-failed"
STALE_SEQUENCE = "stale-sequence"
INVALID_LABEL = "invalid-label"
RESOURCE_EXHAUSTED = "resource-exhausted"
TTL_EXCEEDED = "ttl-exceeded"
UNKNOWN_LABEL = "unknown-label"
SESSION_DOWN = "session-down"


class LdpError(Exception):
    def __init__(self, code, message=""):
        self.code = code
        super().__init__(code if not message else "%s: %s" % (code, message))


class LdpLabelLedger:
    """FEC to label bindings, label pool and per-peer session state.

    authenticate() receives a hex HMAC-SHA256 digest over the message payload
    using the configured session secret.  Released labels stay quarantined
    for quarantine_seconds before they can be handed out again.
    """

    def __init__(self, label_min=16, label_max=1000, secret=None,
                 quarantine_seconds=30):
        if not self._is_int(label_min) or not self._is_int(label_max):
            raise ValueError("label range must be integers")
        if label_min < 0 or label_max < label_min:
            raise ValueError("invalid label range")
        if quarantine_seconds < 0:
            raise ValueError("quarantine_seconds must be non-negative")

        self.label_min = label_min
        self.label_max = label_max
        self.secret = secret
        self.quarantine_seconds = quarantine_seconds
        self._clock = 0

        self._sessions = {}
        self._bindings = {}
        self._leases = {}
        self._used_labels = {}
        self._fec_seq = {}
        self._release_seq = {}
        self._quarantine = {}
        self._free = []
        self._next_fresh = label_min
        self._unreconciled = set()

    @staticmethod
    def _is_int(value):
        return isinstance(value, int) and not isinstance(value, bool)

    def _require_sequence(self, seq):
        if not self._is_int(seq) or seq < 0:
            raise LdpError(STALE_SEQUENCE, "invalid sequence number")

    def _require_active_session(self, peer):
        session = self._sessions.get(peer)
        if session is None or not session["active"]:
            raise LdpError(SESSION_DOWN, "no active session for %s" % (peer,))
        return session

    def _validate_label(self, label):
        if (not self._is_int(label) or label <= 0
                or label < self.label_min or label > self.label_max):
            raise LdpError(
                INVALID_LABEL,
                "label %r is outside [%d, %d]" % (
                    label, self.label_min, self.label_max),
            )

    def _expire_quarantine(self):
        for label, available_at in list(self._quarantine.items()):
            if available_at <= self._clock:
                del self._quarantine[label]
                if label not in self._used_labels:
                    heapq.heappush(self._free, label)

    def _free_label(self, label, quarantine):
        self._used_labels.pop(label, None)
        if quarantine and self.quarantine_seconds > 0:
            self._quarantine[label] = self._clock + self.quarantine_seconds
        else:
            self._quarantine.pop(label, None)
            heapq.heappush(self._free, label)

    def _allocate_label(self):
        self._expire_quarantine()

        while self._free:
            label = heapq.heappop(self._free)
            if label < self.label_min or label > self.label_max:
                continue
            if label in self._used_labels or label in self._quarantine:
                continue
            return label

        while self._next_fresh <= self.label_max:
            label = self._next_fresh
            self._next_fresh += 1
            if label <= 0:
                continue
            if label not in self._used_labels and label not in self._quarantine:
                return label

        raise LdpError(RESOURCE_EXHAUSTED, "label space exhausted")

    def _remove_binding(self, peer, fec, quarantine):
        peer_bindings = self._bindings.get(fec)
        if peer_bindings is None:
            return None
        binding = peer_bindings.pop(peer, None)
        if not peer_bindings:
            del self._bindings[fec]
        if binding is not None:
            owner = self._used_labels.get(binding["label"])
            if (owner is not None and owner["peer"] == peer
                    and owner["fec"] == fec
                    and owner["kind"] == "advertised"):
                self._free_label(binding["label"], quarantine)
        return binding

    def _clear_peer_state(self, peer, quarantine):
        released = set()

        for fec in list(self._bindings):
            binding = self._bindings[fec].get(peer)
            if binding is not None:
                released.add(binding["label"])
                self._remove_binding(peer, fec, quarantine)

        peer_leases = self._leases.pop(peer, {})
        for fec, lease in peer_leases.items():
            released.add(lease["label"])
            owner = self._used_labels.get(lease["label"])
            if owner is not None and owner["peer"] == peer and owner["fec"] == fec:
                self._free_label(lease["label"], quarantine)

        for key in [key for key in self._fec_seq if key[0] == peer]:
            del self._fec_seq[key]
        for key in [key for key in self._release_seq if key[0] == peer]:
            del self._release_seq[key]
        self._unreconciled.discard(peer)

        return sorted(released)

    def open_session(self, peer, transport, password=None):
        if peer is None or transport is None:
            raise ValueError("peer and transport are required")

        current = self._sessions.get(peer)
        if current is not None and current["active"]:
            if current["transport"] == transport:
                if password is not None:
                    current["secret"] = password
                return True
            self._clear_peer_state(peer, quarantine=False)

        self._sessions[peer] = {
            "transport": transport,
            "active": True,
            "secret": password if password is not None else self.secret,
        }
        return True

    def authenticate(self, peer, message_id, payload, digest):
        session = self._sessions.get(peer)
        if session is None or not session["active"]:
            raise LdpError(AUTH_FAILED, "unknown or inactive session")

        secret = session.get("secret")
        if secret is None:
            raise LdpError(AUTH_FAILED, "no authentication secret")
        if isinstance(secret, str):
            secret = secret.encode("utf-8")
        if isinstance(payload, str):
            payload = payload.encode("utf-8")
        if not isinstance(payload, (bytes, bytearray)):
            raise LdpError(AUTH_FAILED, "payload must be bytes")

        if isinstance(digest, bytes):
            try:
                digest = digest.decode("ascii")
            except UnicodeDecodeError:
                raise LdpError(AUTH_FAILED, "invalid digest") from None
        if not isinstance(digest, str):
            raise LdpError(AUTH_FAILED, "invalid digest")

        expected = hmac.new(bytes(secret), bytes(payload),
                            hashlib.sha256).hexdigest()
        try:
            valid = hmac.compare_digest(expected, digest.strip().lower())
        except TypeError:
            valid = False
        if not valid:
            raise LdpError(AUTH_FAILED, "message authentication failed")
        return True

    def advertise(self, peer, fec, label, seq, local_pref=100, metric=10):
        self._require_active_session(peer)
        self._validate_label(label)
        self._require_sequence(seq)
        if fec is None:
            raise ValueError("fec is required")
        if not self._is_int(local_pref) or not self._is_int(metric):
            raise ValueError("local_pref and metric must be integers")
        if metric < 0:
            raise ValueError("metric must be non-negative")

        key = (peer, fec)
        previous = self._fec_seq.get(key)
        current = self._bindings.get(fec, {}).get(peer)

        if previous is not None:
            if seq < previous["seq"]:
                raise LdpError(STALE_SEQUENCE, "advertisement is stale")
            if seq == previous["seq"]:
                if (previous["action"] == "advertise" and current is not None
                        and current["label"] == label
                        and current["local_pref"] == local_pref
                        and current["metric"] == metric):
                    return dict(current)
                raise LdpError(
                    STALE_SEQUENCE,
                    "conflicting duplicate advertisement",
                )

        owner = self._used_labels.get(label)
        if owner is not None:
            same_binding = (
                current is not None
                and current["label"] == label
                and owner["peer"] == peer
                and owner["fec"] == fec
                and owner["kind"] == "advertised"
            )
            if not same_binding:
                raise LdpError(INVALID_LABEL, "label is already in use")

        old_label = current["label"] if current is not None else None
        if old_label is not None and old_label != label:
            self._free_label(old_label, quarantine=True)

        self._quarantine.pop(label, None)
        binding = {
            "fec": fec,
            "peer": peer,
            "label": label,
            "seq": seq,
            "local_pref": local_pref,
            "metric": metric,
        }
        self._bindings.setdefault(fec, {})[peer] = binding
        self._used_labels[label] = {
            "peer": peer,
            "fec": fec,
            "kind": "advertised",
        }
        self._fec_seq[key] = {"seq": seq, "action": "advertise"}
        return dict(binding)

    def withdraw(self, peer, fec, seq):
        self._require_sequence(seq)

        key = (peer, fec)
        previous = self._fec_seq.get(key)
        current = self._bindings.get(fec, {}).get(peer)

        if previous is not None:
            if seq < previous["seq"]:
                raise LdpError(STALE_SEQUENCE, "withdrawal is stale")
            if seq == previous["seq"]:
                if previous["action"] == "withdraw":
                    return True
                raise LdpError(
                    STALE_SEQUENCE,
                    "withdrawal conflicts with advertisement sequence",
                )

        if current is not None:
            self._remove_binding(peer, fec, quarantine=True)

        self._fec_seq[key] = {"seq": seq, "action": "withdraw"}
        return True

    def route(self, fec):
        candidates = self._bindings.get(fec, {})
        usable = [
            binding for peer, binding in candidates.items()
            if peer not in self._unreconciled
        ]
        if not usable:
            return None

        best = min(
            usable,
            key=lambda binding: (
                -binding["local_pref"],
                binding["metric"],
                str(binding["peer"]),
                binding["label"],
                binding["seq"],
            ),
        )
        return dict(best)

    def label_in_use(self, label):
        return label in self._used_labels

    def request_label(self, peer, fec):
        if fec is None:
            raise ValueError("fec is required")

        peer_leases = self._leases.setdefault(peer, {})
        existing = peer_leases.get(fec)
        if existing is not None:
            return existing["label"]

        label = self._allocate_label()
        owner = self._used_labels.get(label)
        if owner is not None:
            raise LdpError(INVALID_LABEL, "allocated label is already in use")

        peer_leases[fec] = {"label": label}
        self._used_labels[label] = {
            "peer": peer,
            "fec": fec,
            "kind": "allocated",
        }
        return label

    def release_label(self, peer, label, seq):
        self._validate_label(label)
        self._require_sequence(seq)

        key = (peer, label)
        previous_seq = self._release_seq.get(key)
        if previous_seq is not None and seq < previous_seq:
            raise LdpError(STALE_SEQUENCE, "label release is stale")
        if previous_seq is not None and seq == previous_seq:
            return True

        owner = self._used_labels.get(label)
        if owner is None:
            if previous_seq is None:
                raise LdpError(UNKNOWN_LABEL, "label was not allocated")
            self._release_seq[key] = seq
            return True

        if owner["kind"] != "allocated" or owner["peer"] != peer:
            raise LdpError(UNKNOWN_LABEL, "label is not owned by this peer")

        lease = self._leases.get(peer, {}).get(owner["fec"])
        if lease is None or lease["label"] != label:
            raise LdpError(UNKNOWN_LABEL, "label lease does not exist")

        del self._leases[peer][owner["fec"]]
        if not self._leases[peer]:
            del self._leases[peer]
        self._free_label(label, quarantine=True)
        self._release_seq[key] = seq
        return True

    def session_down(self, peer):
        session = self._sessions.get(peer)
        if session is not None:
            session["active"] = False
        return self._clear_peer_state(peer, quarantine=False)

    def set_transport(self, peer, transport):
        session = self._sessions.get(peer)
        if session is None:
            raise LdpError(SESSION_DOWN, "unknown peer")
        if session["transport"] == transport:
            return False

        self._clear_peer_state(peer, quarantine=False)
        session["transport"] = transport
        return True

    def transport_of(self, peer):
        session = self._sessions.get(peer)
        return None if session is None else session["transport"]

    def check_ttl(self, ttl):
        if not self._is_int(ttl) or ttl < 2 or ttl > 255:
            raise LdpError(TTL_EXCEEDED, "TTL out of range")
        return ttl - 1

    def handle_notification(self, peer, status_code):
        if not self._is_int(status_code) or status_code < 0:
            raise ValueError("invalid notification status code")
        if status_code == 0:
            return "success"

        fatal = bool(status_code & 0x80000000) or status_code >= 32
        if fatal:
            self.session_down(peer)
            return "fatal"
        return "warning"

    def reconcile(self, peer, retained_fecs):
        retained = set(retained_fecs)
        local_for_peer = {
            fec for fec, peer_bindings in self._bindings.items()
            if peer in peer_bindings
        }

        missing = sorted(local_for_peer - retained)
        extra = sorted(retained - local_for_peer)
        for fec in missing:
            self._remove_binding(peer, fec, quarantine=True)

        self._unreconciled.discard(peer)
        return {"missing": missing, "extra": extra}

    def advance(self, seconds):
        if not self._is_int(seconds) or seconds < 0:
            raise ValueError("seconds must be a non-negative integer")
        self._clock += seconds
        self._expire_quarantine()

    def snapshot(self):
        sessions = []
        for peer, session in self._sessions.items():
            sessions.append({
                "peer": peer,
                "transport": session["transport"],
                "active": bool(session["active"]),
            })

        bindings = []
        for fec in sorted(self._bindings):
            for peer in sorted(self._bindings[fec], key=str):
                bindings.append(dict(self._bindings[fec][peer]))

        leases = []
        for peer in sorted(self._leases, key=str):
            for fec in sorted(self._leases[peer], key=str):
                leases.append({
                    "peer": peer,
                    "fec": fec,
                    "label": self._leases[peer][fec]["label"],
                })

        sequences = [
            {
                "peer": peer,
                "fec": fec,
                "seq": record["seq"],
                "action": record["action"],
            }
            for (peer, fec), record in sorted(
                self._fec_seq.items(),
                key=lambda item: (str(item[0][0]), str(item[0][1])),
            )
        ]
        release_sequences = [
            {"peer": peer, "label": label, "seq": seq}
            for (peer, label), seq in sorted(
                self._release_seq.items(),
                key=lambda item: (str(item[0][0]), item[0][1]),
            )
        ]
        quarantine = [
            {"label": label, "available_at": available_at}
            for label, available_at in sorted(self._quarantine.items())
        ]

        return {
            "version": 1,
            "label_min": self.label_min,
            "label_max": self.label_max,
            "clock": self._clock,
            "quarantine_seconds": self.quarantine_seconds,
            "next_fresh": self._next_fresh,
            "sessions": sessions,
            "bindings": bindings,
            "leases": leases,
            "fec_sequences": sequences,
            "release_sequences": release_sequences,
            "quarantine": quarantine,
            "free": sorted(self._free),
            "unreconciled": sorted(self._unreconciled, key=str),
        }

    def restore(self, snapshot):
        if not isinstance(snapshot, dict) or snapshot.get("version") != 1:
            raise ValueError("unsupported snapshot")

        label_min = snapshot["label_min"]
        label_max = snapshot["label_max"]
        if (not self._is_int(label_min) or not self._is_int(label_max)
                or label_min < 0 or label_max < label_min):
            raise ValueError("invalid label range in snapshot")

        self.label_min = label_min
        self.label_max = label_max
        quarantine_seconds = snapshot.get(
            "quarantine_seconds", self.quarantine_seconds)
        if quarantine_seconds < 0:
            raise ValueError("invalid quarantine_seconds in snapshot")
        self.quarantine_seconds = quarantine_seconds
        self._clock = snapshot.get("clock", 0)
        self._sessions = {}
        self._bindings = {}
        self._leases = {}
        self._used_labels = {}
        self._fec_seq = {}
        self._release_seq = {}
        self._quarantine = {}
        self._free = []
        self._next_fresh = snapshot.get("next_fresh", label_min)
        self._unreconciled = set(snapshot.get("unreconciled", []))

        for session in snapshot.get("sessions", []):
            peer = session["peer"]
            self._sessions[peer] = {
                "transport": session["transport"],
                "active": bool(session.get("active", True)),
                "secret": self.secret,
            }

        for binding in snapshot.get("bindings", []):
            label = binding["label"]
            self._validate_label(label)
            if label in self._used_labels:
                raise LdpError(INVALID_LABEL, "duplicate label in snapshot")
            fec = binding["fec"]
            peer = binding["peer"]
            if peer in self._bindings.get(fec, {}):
                raise LdpError(INVALID_LABEL, "duplicate FEC in snapshot")
            stored = {
                "fec": fec,
                "peer": peer,
                "label": label,
                "seq": binding["seq"],
                "local_pref": binding["local_pref"],
                "metric": binding["metric"],
            }
            self._bindings.setdefault(fec, {})[peer] = stored
            self._used_labels[label] = {
                "peer": peer,
                "fec": fec,
                "kind": "advertised",
            }

        for lease in snapshot.get("leases", []):
            label = lease["label"]
            self._validate_label(label)
            if label in self._used_labels:
                raise LdpError(INVALID_LABEL, "duplicate label in snapshot")
            peer = lease["peer"]
            fec = lease["fec"]
            if fec in self._leases.get(peer, {}):
                raise LdpError(INVALID_LABEL, "duplicate lease in snapshot")
            self._leases.setdefault(peer, {})[fec] = {"label": label}
            self._used_labels[label] = {
                "peer": peer,
                "fec": fec,
                "kind": "allocated",
            }

        for record in snapshot.get("fec_sequences", []):
            self._fec_seq[(record["peer"], record["fec"])] = {
                "seq": record["seq"],
                "action": record["action"],
            }
        for record in snapshot.get("release_sequences", []):
            self._release_seq[(record["peer"], record["label"])] = record["seq"]

        for record in snapshot.get("quarantine", []):
            label = record["label"]
            self._validate_label(label)
            if label in self._used_labels:
                raise LdpError(INVALID_LABEL, "active label is quarantined")
            self._quarantine[label] = record["available_at"]

        for label in snapshot.get("free", []):
            self._validate_label(label)
            if label in self._used_labels or label in self._quarantine:
                continue
            heapq.heappush(self._free, label)

        if not self._is_int(self._next_fresh):
            raise ValueError("invalid next_fresh in snapshot")

        # A restarted process may not forward retained bindings until the
        # neighbor's retained FEC set has been checked entry by entry.
        for peer_bindings in self._bindings.values():
            self._unreconciled.update(peer_bindings)
        self._unreconciled.update(
            peer for peer, leases in self._leases.items() if leases
        )
