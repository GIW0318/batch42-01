"""WPA3 SAE commit/confirm scheduler.

The scheduler keeps one handshake state machine per STA.  Every Commit and
Confirm for a STA is serialised on a per-STA lock, anti-clogging tokens are
bound to the STA they were issued for and consumed by the first valid
Commit, retransmissions are idempotent and key installation happens in the
same locked step as the transition to the authenticated state.

Handshake deadlines use a wall-clock based timeline (monotonic offsets over
``time.time``) so that half-open state survives a process restart through
:meth:`snapshot`/:meth:`restore` and is still reaped once it expires.
"""

import hmac
import threading
import time
import uuid

ANTI_CLOGGING = "anti-clogging"
TOKEN_MISMATCH = "token-mismatch"
GROUP_NOT_ALLOWED = "group-not-allowed"
INVALID_LENGTH = "invalid-length"
CONFIRM_MISMATCH = "confirm-mismatch"
AUTHENTICATION_FAILED = "authentication-failed"
INVALID_STATE = "invalid-state"
STA_BUSY = "sta-busy"


class SaeError(Exception):
    def __init__(self, code, message="", field=None):
        self.code = code
        self.field = field
        super().__init__(code if not message else "%s: %s" % (code, message))


class SaeCommitScheduler:
    """Per-STA SAE handshake state with anti-clogging tokens.

    Commit scalars are 32 bytes and elements 64 bytes.  A Commit without a
    token is rejected before any group arithmetic, retransmissions are
    idempotent per (STA, token) and key installation happens together with
    the transition to authenticated state.
    """

    def __init__(self, allowed_groups=(19, 20), commit_timeout=30,
                 credentials=None, pmksa_ttl=3600):
        self.allowed_groups = tuple(allowed_groups)
        self.commit_timeout = commit_timeout
        self.credentials = dict(credentials or {})
        self.pmksa_ttl = pmksa_ttl
        self._offset = 0.0
        self._guard = threading.RLock()
        self._sta_locks = {}
        self._tokens = {}
        self._sessions = {}
        self._tombstones = {}
        self._pmksa = {}

    def _now(self):
        return time.time() + self._offset

    def _lock_for(self, sta):
        with self._guard:
            lock = self._sta_locks.get(sta)
            if lock is None:
                lock = threading.RLock()
                self._sta_locks[sta] = lock
            return lock

    @staticmethod
    def _public_session(session):
        return {
            "session_id": session["session_id"],
            "state": session["state"],
            "group": session["group"],
            "send_id": session["send_id"],
            "recv_id": session["recv_id"],
        }

    def issue_token(self, sta):
        token = uuid.uuid4().hex
        with self._guard:
            self._tokens[token] = sta
        return token

    def commit(self, sta, token, group=19, scalar=b"", element=b"",
               password=None, ssid=None):
        if not token:
            raise SaeError(ANTI_CLOGGING, "commit rejected before any "
                                          "group arithmetic")

        with self._lock_for(sta):
            session = self._sessions.get(sta)
            if session is not None and session.get("token") == token:
                if session["state"] != "committed":
                    raise SaeError(INVALID_STATE, "handshake already "
                                                   "authenticated")
                if (session["scalar"] == scalar
                        and session["element"] == element
                        and session["group"] == group):
                    return self._public_session(session)
                raise SaeError(TOKEN_MISMATCH, "token already consumed by "
                                               "a different commit")

            tombstone = self._tombstones.get(sta)
            if tombstone is not None and tombstone.get("token") == token:
                raise SaeError(TOKEN_MISMATCH, "token belongs to an expired "
                                               "handshake")

            bound_sta = self._tokens.get(token)
            if bound_sta is None:
                raise SaeError(TOKEN_MISMATCH, "unknown or reused token")
            if bound_sta != sta:
                raise SaeError(TOKEN_MISMATCH, "token is bound to another "
                                               "STA", field="token")

            if group not in self.allowed_groups:
                raise SaeError(GROUP_NOT_ALLOWED,
                               "group %r is not in the allowed list"
                               % (group,), field="group")
            if not isinstance(scalar, (bytes, bytearray)) or len(scalar) != 32:
                raise SaeError(INVALID_LENGTH,
                               "commit scalar must be exactly 32 bytes",
                               field="scalar")
            if not isinstance(element, (bytes, bytearray)) or len(element) != 64:
                raise SaeError(INVALID_LENGTH,
                               "commit element must be exactly 64 bytes",
                               field="element")

            expected = self.credentials.get(sta)
            if expected is None or not hmac.compare_digest(
                    str(expected), str(password)):
                raise SaeError(AUTHENTICATION_FAILED, "credential check "
                                                      "failed")

            del self._tokens[token]
            self._tombstones.pop(sta, None)
            new_session = {
                "session_id": uuid.uuid4().hex,
                "state": "committed",
                "group": group,
                "scalar": bytes(scalar),
                "element": bytes(element),
                "token": token,
                "send_id": uuid.uuid4().hex,
                "recv_id": uuid.uuid4().hex,
                "deadline": self._now() + self.commit_timeout,
                "key_installs": 0,
                "confirm_result": None,
            }
            self._sessions[sta] = new_session
            return self._public_session(new_session)

    def confirm(self, sta, send_id, recv_id, confirm=b""):
        with self._lock_for(sta):
            session = self._sessions.get(sta)
            if session is None:
                tombstone = self._tombstones.get(sta)
                if tombstone is not None and (
                        tombstone["send_id"] == send_id
                        and tombstone["recv_id"] == recv_id):
                    raise SaeError(AUTHENTICATION_FAILED,
                                   "handshake timed out before confirmation")
                raise SaeError(INVALID_STATE,
                               "no committed handshake for STA")

            if session["state"] == "committed" and session["deadline"] <= self._now():
                raise SaeError(AUTHENTICATION_FAILED,
                               "handshake timed out before confirmation")

            if (session["send_id"] != send_id
                    or session["recv_id"] != recv_id):
                raise SaeError(CONFIRM_MISMATCH,
                               "confirm identifiers do not match the "
                               "committed session",
                               field="send_id")

            if session["state"] == "authenticated":
                result = dict(session["confirm_result"])
                result["duplicate"] = True
                return result

            result = {
                "session_id": session["session_id"],
                "state": "authenticated",
            }
            session["state"] = "authenticated"
            session["key_installs"] = 1
            session["confirm_result"] = dict(result)
            return result

    def session_state(self, sta):
        with self._lock_for(sta):
            session = self._sessions.get(sta)
            return session["state"] if session is not None else "none"

    def current_group(self, sta):
        with self._lock_for(sta):
            session = self._sessions.get(sta)
            return session["group"] if session is not None else None

    def key_install_count(self, sta):
        with self._lock_for(sta):
            session = self._sessions.get(sta)
            return session["key_installs"] if session is not None else 0

    def advance(self, seconds):
        self._offset += seconds

    def expire(self):
        expired = []
        now = self._now()
        with self._guard:
            for sta in list(self._sessions):
                with self._lock_for(sta):
                    session = self._sessions.get(sta)
                    if (session is not None
                            and session["state"] == "committed"
                            and session["deadline"] <= now):
                        self._tombstones[sta] = {
                            "token": session["token"],
                            "send_id": session["send_id"],
                            "recv_id": session["recv_id"],
                            "deadline": session["deadline"],
                        }
                        del self._sessions[sta]
                        expired.append(sta)

            for sta, networks in list(self._pmksa.items()):
                for ssid, entries in list(networks.items()):
                    for pmkid in list(entries):
                        if entries[pmkid]["expires"] <= now:
                            del entries[pmkid]
                    if not entries:
                        del networks[ssid]
                if not networks:
                    del self._pmksa[sta]
        return expired

    def cache_pmksa(self, sta, ssid, pmkid, pmk):
        with self._lock_for(sta):
            entry = {
                "pmk": bytes(pmk),
                "expires": self._now() + self.pmksa_ttl,
            }
            self._pmksa.setdefault(sta, {}).setdefault(ssid, {})[pmkid] = entry

    def lookup_pmksa(self, sta, ssid, pmkid):
        with self._lock_for(sta):
            entry = self._pmksa.get(sta, {}).get(ssid, {}).get(pmkid)
            if entry is None or entry["expires"] <= self._now():
                return False
            return True

    def snapshot(self):
        with self._guard:
            sessions = {}
            for sta, session in self._sessions.items():
                saved = dict(session)
                saved["scalar"] = session["scalar"].hex()
                saved["element"] = session["element"].hex()
                sessions[sta] = saved
            pmksa = {}
            for sta, networks in self._pmksa.items():
                pmksa[sta] = {}
                for ssid, entries in networks.items():
                    pmksa[sta][ssid] = {
                        pmkid: {
                            "pmk": entry["pmk"].hex(),
                            "expires": entry["expires"],
                        }
                        for pmkid, entry in entries.items()
                    }
            return {
                "now": self._now(),
                "tokens": dict(self._tokens),
                "sessions": sessions,
                "tombstones": {
                    sta: dict(tombstone)
                    for sta, tombstone in self._tombstones.items()
                },
                "pmksa": pmksa,
            }

    def restore(self, snapshot):
        with self._guard:
            self._offset = snapshot["now"] - time.time()
            self._tokens = dict(snapshot.get("tokens", {}))
            self._tombstones = {
                sta: dict(tombstone)
                for sta, tombstone in snapshot.get("tombstones", {}).items()
            }
            self._sessions = {}
            for sta, saved in snapshot.get("sessions", {}).items():
                session = dict(saved)
                session["scalar"] = bytes.fromhex(saved["scalar"])
                session["element"] = bytes.fromhex(saved["element"])
                self._sessions[sta] = session
            self._pmksa = {}
            for sta, networks in snapshot.get("pmksa", {}).items():
                self._pmksa[sta] = {}
                for ssid, entries in networks.items():
                    self._pmksa[sta][ssid] = {
                        pmkid: {
                            "pmk": bytes.fromhex(entry["pmk"]),
                            "expires": entry["expires"],
                        }
                        for pmkid, entry in entries.items()
                    }
