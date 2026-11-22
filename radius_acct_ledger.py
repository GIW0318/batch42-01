"""RADIUS accounting ledger.

Contract notes for the implementation:

* ``make_authenticator`` derives a stable digest from the NAS shared secret
  and the normalised packet fields.  ``process`` drops a packet whose
  authenticator does not match the secret of its NAS: status ``rejected``,
  reason ``bad authenticator`` and no ledger row.
* Session identity is ``(nas, session_id.strip().lower(),
  user.strip().lower())``.
* A repeated Start for an open session (or a closed record with the same
  ``session_time``) returns ``duplicate_start`` and creates no second row.
  A Start with the same id but a new ``session_time`` after the previous
  record closed starts a new session.
* Interims never regress usage: a lower octet count returns
  ``stale_interim`` and keeps the higher value.  An Interim after Stop
  returns ``late_interim`` and cannot reopen the session.
* Stop bills exactly once.  Repeated Stop returns ``duplicate_stop`` with
  ``billed`` False.  Stop before any Interim bills the Stop octet count.
* When ``min_interim_interval`` is positive, an Interim arriving too early
  is coalesced: status ``coalesced`` but usage still takes the maximum.
* Unknown accounting status is rejected as ``unknown_status`` with no row.
* ``export_state`` / ``load_state`` preserve open and closed sessions plus
  billed totals so restart keeps Stop idempotency.
"""

import copy
import hashlib
import hmac
import threading


_KNOWN_STATUSES = {
    "start": "start",
    "interim": "interim",
    "interim-update": "interim",
    "stop": "stop",
}


class RadiusAcctLedger:
    def __init__(self, secrets=None, min_interim_interval=0.0):
        self._secrets = dict(secrets or {})
        self._min_interim_interval = max(0.0, float(min_interim_interval))
        self._lock = threading.RLock()
        self._records = []
        self._latest = {}

    @staticmethod
    def _normalise_session_id(session_id):
        return str(session_id if session_id is not None else "").strip().lower()

    @staticmethod
    def _normalise_user(user):
        return str(user if user is not None else "").strip().lower()

    @staticmethod
    def _normalise_status(status):
        return str(status if status is not None else "").strip().lower()

    @staticmethod
    def _as_int(value, default=0):
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _as_float(value, default=0.0):
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _key(nas, session_id, user):
        return (nas, session_id, user)

    def _authenticator_for(self, nas, session_id, status, octets, session_time):
        try:
            secret = self._secrets[nas]
        except (KeyError, TypeError):
            return None

        fields = (
            str(nas),
            self._normalise_session_id(session_id),
            self._normalise_status(status),
            str(self._as_int(octets)),
            str(self._as_int(session_time)),
        )
        message = "\x1f".join(fields).encode("utf-8")
        key = str(secret).encode("utf-8")
        return hmac.new(key, message, hashlib.sha256).hexdigest()

    def make_authenticator(
        self, nas, session_id, status, octets, session_time=0
    ):
        authenticator = self._authenticator_for(
            nas, session_id, status, octets, session_time
        )
        if authenticator is None:
            raise ValueError("unknown NAS")
        return authenticator

    def _append_record(self, nas, session_id, user, session_time, usage, now):
        record = {
            "nas": nas,
            "session_id": session_id,
            "user": user,
            "session_time": session_time,
            "usage": max(0, usage),
            "open": True,
            "billed": False,
            "last_interim_at": self._as_float(now),
        }
        self._records.append(record)
        self._latest[self._key(nas, session_id, user)] = record
        return record

    @staticmethod
    def _result(status, created=False, billed=False, reason=None):
        return {
            "code": "Accounting-Response",
            "status": status,
            "created": created,
            "billed": billed,
            "reason": reason,
        }

    def _find_latest(self, nas, session_id, user):
        return self._latest.get(self._key(nas, session_id, user))

    def process(self, packet, now=0.0):
        if not isinstance(packet, dict):
            return self._result("rejected", reason="malformed packet")

        nas = packet.get("nas")
        session_id = self._normalise_session_id(packet.get("session_id"))
        user = self._normalise_user(packet.get("user"))
        status = self._normalise_status(packet.get("status"))
        raw_octets = self._as_int(packet.get("octets"))
        session_time = self._as_int(packet.get("session_time"))

        expected = self._authenticator_for(
            nas,
            session_id,
            packet.get("status"),
            raw_octets,
            session_time,
        )
        provided = packet.get("authenticator")
        if (
            expected is None
            or not isinstance(provided, str)
            or not hmac.compare_digest(provided, expected)
        ):
            return self._result("rejected", reason="bad authenticator")

        if status not in _KNOWN_STATUSES:
            return self._result("unknown_status")

        status = _KNOWN_STATUSES[status]
        octets = max(0, raw_octets)
        arrival = self._as_float(now)

        with self._lock:
            latest = self._find_latest(nas, session_id, user)

            if status == "start":
                if latest is not None and (
                    latest["open"]
                    or latest["session_time"] == session_time
                ):
                    return self._result("duplicate_start")

                self._append_record(
                    nas, session_id, user, session_time, octets, arrival
                )
                return self._result("started", created=True)

            if status == "interim":
                if latest is not None and not latest["open"]:
                    return self._result("late_interim")

                if latest is None:
                    latest = self._append_record(
                        nas, session_id, user, session_time, octets, arrival
                    )
                    return self._result("updated", created=True)

                if octets < latest["usage"]:
                    return self._result("stale_interim")

                previous_arrival = self._as_float(
                    latest.get("last_interim_at"), arrival
                )
                if (
                    self._min_interim_interval > 0
                    and arrival - previous_arrival < self._min_interim_interval
                ):
                    latest["usage"] = max(latest["usage"], octets)
                    return self._result("coalesced")

                latest["usage"] = max(latest["usage"], octets)
                latest["last_interim_at"] = arrival
                return self._result("updated")

            if latest is not None and not latest["open"]:
                return self._result("duplicate_stop")

            if latest is None:
                latest = self._append_record(
                    nas, session_id, user, session_time, octets, arrival
                )

            latest["usage"] = max(latest["usage"], octets)
            latest["open"] = False
            latest["billed"] = True
            return self._result("stopped", billed=True)

    def session_usage(self, nas, session_id):
        session_id = self._normalise_session_id(session_id)
        with self._lock:
            for record in reversed(self._records):
                if (
                    record["nas"] == nas
                    and record["session_id"] == session_id
                ):
                    return record["usage"]
        return 0

    def billed_total(self, user):
        user = self._normalise_user(user)
        with self._lock:
            return sum(
                record["usage"]
                for record in self._records
                if record["user"] == user and record["billed"]
            )

    def open_sessions(self):
        with self._lock:
            return [
                copy.deepcopy(record)
                for record in self._records
                if record["open"]
            ]

    def session_count(self):
        with self._lock:
            return len(self._records)

    def export_state(self):
        with self._lock:
            return {
                "version": 1,
                "records": copy.deepcopy(self._records),
            }

    def load_state(self, state):
        if isinstance(state, dict):
            records = state.get("records", [])
        else:
            records = state or []

        restored = []
        latest = {}
        for item in records:
            record = copy.deepcopy(item)
            record["session_id"] = self._normalise_session_id(
                record.get("session_id")
            )
            record["user"] = self._normalise_user(record.get("user"))
            record["session_time"] = self._as_int(record.get("session_time"))
            record["usage"] = max(0, self._as_int(record.get("usage")))
            record["open"] = bool(record.get("open"))
            record["billed"] = bool(record.get("billed"))
            record["last_interim_at"] = self._as_float(
                record.get("last_interim_at")
            )
            restored.append(record)
            latest[
                self._key(
                    record.get("nas"),
                    record["session_id"],
                    record["user"],
                )
            ] = record

        with self._lock:
            self._records = restored
            self._latest = latest
