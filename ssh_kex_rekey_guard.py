"""SSH transport rekey guard.

Baseline shell: every method raises NotImplementedError.

Contract notes for the implementation:

* ``open_connection`` picks the strongest common algorithm from
  ``("curve25519-sha256", "ecdh-sha2-nistp256",
  "diffie-hellman-group14-sha256")``.  If there is no common strong
  algorithm the connection is ``disconnected`` with reason exactly
  ``no matching key exchange method``.
* ``begin_rekey`` is single-flight per connection and bounded by
  ``max_pending``.  A second concurrent request returns ``accepted`` False
  with reason ``rekey in progress``; when the global pending budget is full
  the reason is ``rekey queue full``.
* ``send_data`` while a rekey is pending is refused (``sent`` False,
  ``buffered`` True, ``violation`` True) until ``complete_rekey`` swaps in
  the new key.  Sequence numbers continue across a rekey, they are never
  reset to zero, and key ids move ``k1`` -> ``k2`` only after NEWKEYS.
* ``consume_window`` refuses to move bytes while a rekey is pending
  (``window frozen``); after ``complete_rekey`` each channel window is
  recomputed from the negotiated initial window.
* ``advance_bytes`` uses a 64-bit counter, so wrapping past 2**32 still
  crosses the configured rekey threshold.
* ``check_timeout`` aborts a stalled rekey and closes the connection,
  releasing its channels and handles; nothing stays half-open.
* ``rotate_host_key`` replaces the trusted host key, so the previous key
  fails ``check_host_key`` with reason ``host key changed``.
* Debug entries never contain raw KEX payloads or session ids; they may
  contain lengths, hashes or redaction markers.
"""

import hashlib


STRONG_ALGORITHMS = (
    "curve25519-sha256",
    "ecdh-sha2-nistp256",
    "diffie-hellman-group14-sha256",
)

NO_COMMON_ALGORITHM = "no matching key exchange method"


class SshRekeyGuard:
    def __init__(self, rekey_bytes=1024 * 1024, rekey_timeout=30.0, max_pending=8):
        self.rekey_bytes = rekey_bytes
        self.rekey_timeout = rekey_timeout
        self.max_pending = max_pending
        self._connections = {}
        self._channel_index = {}
        self._host_keys = {}
        self._rekey_sequence = 0
        self._debug = False
        self._debug_entries = []

    def open_connection(
        self, conn_id, client_algorithms, server_algorithms, session_id=None
    ):
        algorithm = None
        for candidate in STRONG_ALGORITHMS:
            if candidate in client_algorithms and candidate in server_algorithms:
                algorithm = candidate
                break
        if algorithm is None:
            self._log(
                "open_connection",
                conn_id=conn_id,
                state="disconnected",
                reason=NO_COMMON_ALGORITHM,
                session_id=self._redact(session_id),
            )
            return {
                "state": "disconnected",
                "reason": NO_COMMON_ALGORITHM,
                "algorithm": None,
            }
        self._connections[conn_id] = {
            "open": True,
            "algorithm": algorithm,
            "key_index": 1,
            "sequence": 0,
            "bytes": 0,
            "violations": 0,
            "channels": {},
            "pending_rekey": None,
        }
        self._log(
            "open_connection",
            conn_id=conn_id,
            state="established",
            algorithm=algorithm,
            session_id=self._redact(session_id),
        )
        return {
            "state": "established",
            "reason": None,
            "algorithm": algorithm,
            "conn_id": conn_id,
        }

    def is_open(self, conn_id):
        conn = self._connections.get(conn_id)
        return bool(conn and conn["open"])

    def begin_rekey(self, conn_id, initiator="client", kexinit=None, now=0.0):
        conn = self._connections.get(conn_id)
        if conn is None or not conn["open"]:
            return {"accepted": False, "reason": "connection closed"}
        if conn["pending_rekey"] is not None:
            return {"accepted": False, "reason": "rekey in progress"}
        if self.pending_rekeys() >= self.max_pending:
            return {"accepted": False, "reason": "rekey queue full"}
        self._rekey_sequence += 1
        rekey_id = "r{}".format(self._rekey_sequence)
        conn["pending_rekey"] = {
            "rekey_id": rekey_id,
            "initiator": initiator,
            "started": now,
        }
        self._log(
            "begin_rekey",
            conn_id=conn_id,
            rekey_id=rekey_id,
            initiator=initiator,
            kexinit=self._redact(kexinit),
        )
        return {"accepted": True, "rekey_id": rekey_id, "initiator": initiator}

    def complete_rekey(self, conn_id, rekey_id):
        conn = self._connections.get(conn_id)
        pending = conn["pending_rekey"] if conn else None
        if pending is None or pending["rekey_id"] != rekey_id:
            return {"completed": False, "reason": "no matching rekey"}
        conn["pending_rekey"] = None
        conn["key_index"] += 1
        for channel in conn["channels"].values():
            channel["window"] = channel["initial_window"]
        key_id = "k{}".format(conn["key_index"])
        self._log(
            "complete_rekey", conn_id=conn_id, rekey_id=rekey_id, key_id=key_id
        )
        return {"completed": True, "rekey_id": rekey_id, "key_id": key_id}

    def abort_rekey(self, conn_id, rekey_id):
        conn = self._connections.get(conn_id)
        pending = conn["pending_rekey"] if conn else None
        if pending is None or pending["rekey_id"] != rekey_id:
            return {"aborted": False, "reason": "no matching rekey"}
        conn["pending_rekey"] = None
        self._log("abort_rekey", conn_id=conn_id, rekey_id=rekey_id)
        return {"aborted": True, "rekey_id": rekey_id}

    def send_data(self, conn_id, nbytes=1):
        conn = self._connections.get(conn_id)
        if conn is None or not conn["open"]:
            return {"sent": False, "buffered": False, "violation": False}
        if conn["pending_rekey"] is not None:
            conn["violations"] += 1
            self._log(
                "send_data_blocked",
                conn_id=conn_id,
                nbytes=nbytes,
                reason="rekey in progress",
            )
            return {"sent": False, "buffered": True, "violation": True}
        conn["sequence"] += 1
        return {"sent": True, "buffered": False, "violation": False}

    def violations(self, conn_id):
        conn = self._connections.get(conn_id)
        return conn["violations"] if conn else 0

    def active_key_id(self, conn_id):
        conn = self._connections.get(conn_id)
        if conn is None:
            return None
        return "k{}".format(conn["key_index"])

    def next_sequence(self, conn_id):
        conn = self._connections.get(conn_id)
        return conn["sequence"] if conn else 0

    def advance_bytes(self, conn_id, nbytes):
        conn = self._connections.get(conn_id)
        if conn is None or not conn["open"]:
            return {"counter": 0, "needs_rekey": False}
        conn["bytes"] = (conn["bytes"] + nbytes) % (2 ** 64)
        counter = conn["bytes"]
        needs = counter >= self.rekey_bytes and nbytes <= self.rekey_bytes
        return {"counter": counter, "needs_rekey": needs}

    def counter_bytes(self, conn_id):
        conn = self._connections.get(conn_id)
        return conn["bytes"] if conn else 0

    def needs_rekey(self, conn_id):
        conn = self._connections.get(conn_id)
        return bool(conn and conn["bytes"] >= self.rekey_bytes)

    def open_channel(self, conn_id, channel_id, window=1024):
        conn = self._connections.get(conn_id)
        if conn is None or not conn["open"]:
            return {"opened": False, "reason": "connection closed"}
        conn["channels"][channel_id] = {
            "window": window,
            "initial_window": window,
        }
        self._channel_index[channel_id] = conn_id
        return {"opened": True, "channel_id": channel_id, "window": window}

    def channel_window(self, channel_id):
        channel = self._find_channel(channel_id)
        return channel["window"] if channel else None

    def consume_window(self, channel_id, nbytes):
        channel = self._find_channel(channel_id)
        if channel is None:
            return {"accepted": False, "reason": "unknown channel", "window": 0}
        conn = self._connections.get(self._channel_index.get(channel_id))
        if conn is not None and conn["pending_rekey"] is not None:
            return {
                "accepted": False,
                "reason": "window frozen",
                "window": channel["window"],
            }
        if nbytes > channel["window"]:
            return {
                "accepted": False,
                "reason": "window exceeded",
                "window": channel["window"],
            }
        channel["window"] -= nbytes
        return {"accepted": True, "window": channel["window"]}

    def close_channel(self, conn_id, channel_id):
        conn = self._connections.get(conn_id)
        if conn is None or channel_id not in conn["channels"]:
            return {"closed": False, "reason": "unknown channel"}
        del conn["channels"][channel_id]
        self._channel_index.pop(channel_id, None)
        return {"closed": True, "channel_id": channel_id}

    def channel_count(self, conn_id):
        conn = self._connections.get(conn_id)
        return len(conn["channels"]) if conn else 0

    def handle_count(self):
        return sum(len(conn["channels"]) for conn in self._connections.values())

    def check_timeout(self, now):
        aborted = []
        closed = []
        for conn_id, conn in self._connections.items():
            pending = conn["pending_rekey"]
            if pending is None:
                continue
            if now - pending["started"] < self.rekey_timeout:
                continue
            conn["pending_rekey"] = None
            aborted.append(conn_id)
            self._close_connection(conn_id, conn)
            closed.append(conn_id)
            self._log(
                "rekey_timeout",
                conn_id=conn_id,
                rekey_id=pending["rekey_id"],
            )
        return {"aborted": aborted, "closed": closed}

    def pending_rekeys(self):
        return sum(
            1
            for conn in self._connections.values()
            if conn["pending_rekey"] is not None
        )

    def rotate_host_key(self, host, new_key):
        self._host_keys[host] = new_key
        self._log("rotate_host_key", host=host, key=self._redact(new_key))
        return {"rotated": True, "host": host}

    def trust_host_key(self, host, key):
        self._host_keys[host] = key
        return {"trusted": True, "host": host}

    def check_host_key(self, host, presented_key):
        trusted = self._host_keys.get(host)
        if trusted is None:
            return {"trusted": False, "reason": "unknown host"}
        if presented_key == trusted:
            return {"trusted": True}
        return {"trusted": False, "reason": "host key changed"}

    def set_debug(self, enabled=True):
        self._debug = enabled

    def debug_entries(self):
        return list(self._debug_entries)

    def _find_channel(self, channel_id):
        conn = self._connections.get(self._channel_index.get(channel_id))
        if conn is None:
            return None
        return conn["channels"].get(channel_id)

    def _close_connection(self, conn_id, conn):
        conn["open"] = False
        for channel_id in conn["channels"]:
            self._channel_index.pop(channel_id, None)
        conn["channels"] = {}

    def _log(self, event, **fields):
        if not self._debug:
            return
        entry = {"event": event}
        entry.update(fields)
        self._debug_entries.append(entry)

    def _redact(self, value):
        if value is None:
            return None
        text = str(value)
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        return "[redacted len={} sha256={}]".format(len(text), digest[:16])
