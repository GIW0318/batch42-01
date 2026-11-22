"""NETCONF candidate/running lock, commit and rollback skeleton."""

import threading

LOCK_DENIED = "lock-denied"
STALE_VERSION = "stale-version"
ROLLBACK_ON_ERROR = "rollback-on-error"
INVALID_VALUE = "invalid-value"
OPERATION_FAILED = "operation-failed"
SENSITIVE_NODES = ("private-key", "community")
DATASTORES = ("candidate", "running")


class NetconfError(Exception):
    def __init__(self, code, message="", errors=None):
        self.code = code
        self.errors = list(errors or [])
        super().__init__(code if not message else "%s: %s" % (code, message))


def _path_segments(path):
    return [segment for segment in str(path).strip("/").split("/") if segment]


class NetconfCandidateLock:
    """Session-scoped NETCONF edit lock with candidate/running commit.

    Lock semantics: a lock on ``running`` protects both running and the
    candidate datastore; a lock on ``candidate`` protects only the candidate.
    Every lock is owned by its session and is released when the session
    closes.  Commits compare the caller-supplied config version against the
    current version, apply all nodes or roll back on any failure, and a
    confirmed commit keeps its deadline in the persisted snapshot so the
    unconfirmed config still rolls back after a process restart.
    """

    def __init__(self, confirmed_timeout=600, defaults=None, initial_running=None,
                 sensitive_nodes=None):
        self.confirmed_timeout = confirmed_timeout
        self.defaults = dict(defaults or {})
        self.initial_running = dict(initial_running or {})
        self.sensitive_nodes = tuple(sensitive_nodes or SENSITIVE_NODES)
        self._clock = 0
        self._version = 0
        self._sessions = {}
        self._locks = {}
        self._candidate = {}
        self._running = dict(self.initial_running)
        self._last_commit = {}
        self._pending = None
        self._guard = threading.RLock()

    def _require_lock(self, session_id, datastore):
        holder = self._locks.get(datastore)
        if holder != session_id:
            raise NetconfError(
                LOCK_DENIED,
                "session %s does not hold the %s lock" % (session_id, datastore),
            )

    def _require_candidate_access(self, session_id):
        running_holder = self._locks.get("running")
        if running_holder == session_id:
            return
        candidate_holder = self._locks.get("candidate")
        if candidate_holder == session_id:
            return
        raise NetconfError(
            LOCK_DENIED,
            "session %s holds no candidate/running lock" % session_id,
        )

    def _is_sensitive(self, path):
        segments = set(_path_segments(path))
        return any(node in segments for node in self.sensitive_nodes)

    def _due_pending(self):
        if self._pending is not None and self._clock >= self._pending["deadline"]:
            self._running = dict(self._pending["rollback_running"])
            event = {
                "status": "rolled-back",
                "deadline": self._pending["deadline"],
                "version": self._pending["version"],
            }
            self._pending = None
            return event
        return None

    def _expire_pending(self):
        events = []
        while True:
            event = self._due_pending()
            if event is None:
                return events
            events.append(event)

    def open_session(self, session_id):
        with self._guard:
            self._sessions.setdefault(session_id, {"locks": set()})
            return {"session": session_id, "status": "open"}

    def close_session(self, session_id):
        with self._guard:
            session = self._sessions.pop(session_id, None)
            if session is None:
                return {"session": session_id, "status": "closed"}
            for datastore in DATASTORES:
                if self._locks.get(datastore) == session_id:
                    del self._locks[datastore]
            self._last_commit.pop(session_id, None)
            return {"session": session_id, "status": "closed"}

    def lock(self, session_id, datastore="running"):
        with self._guard:
            if session_id not in self._sessions:
                raise NetconfError(
                    LOCK_DENIED, "unknown session %s" % session_id
                )
            if datastore not in DATASTORES:
                raise NetconfError(
                    LOCK_DENIED, "unknown datastore %s" % datastore
                )
            guarded = ("candidate", "running") if datastore == "running" else (
                "candidate",
            )
            for name in guarded:
                holder = self._locks.get(name)
                if holder is not None and holder != session_id:
                    raise NetconfError(
                        LOCK_DENIED,
                        "%s lock is held by session %s" % (name, holder),
                    )
            for name in guarded:
                self._locks[name] = session_id
                self._sessions[session_id]["locks"].add(name)
            return sorted(
                name for name, owner in self._locks.items() if owner == session_id
            )

    def unlock(self, session_id, datastore="running"):
        with self._guard:
            self._require_lock(session_id, datastore)
            guarded = ("candidate", "running") if datastore == "running" else (
                "candidate",
            )
            for name in guarded:
                if self._locks.get(name) == session_id:
                    del self._locks[name]
                    self._sessions[session_id]["locks"].discard(name)
            return sorted(
                name for name, owner in self._locks.items() if owner == session_id
            )

    def edit_candidate(self, session_id, changes):
        with self._guard:
            self._expire_pending()
            self._require_candidate_access(session_id)
            for path, value in dict(changes).items():
                if self.defaults.get(path) == value:
                    continue
                self._candidate[path] = value
            self._version += 1
            return self._version

    def get_candidate(self, session_id=None):
        with self._guard:
            self._expire_pending()
            return dict(self._candidate)

    def get_running(self, session_id=None):
        with self._guard:
            self._expire_pending()
            return dict(self._running)

    def discard_changes(self, session_id, expected_version=None):
        with self._guard:
            self._expire_pending()
            self._require_candidate_access(session_id)
            if expected_version is not None and expected_version != self._version:
                raise NetconfError(
                    STALE_VERSION,
                    "expected version %s, current version %s"
                    % (expected_version, self._version),
                )
            discarded = sorted(self._candidate)
            self._candidate = {}
            return discarded

    def copy_config(self, session_id, source="running", target="candidate"):
        with self._guard:
            self._expire_pending()
            if source != "running" or target != "candidate":
                raise NetconfError(
                    OPERATION_FAILED,
                    "only running -> candidate copy-config is supported",
                )
            self._require_candidate_access(session_id)
            filtered = {
                path: value
                for path, value in self._running.items()
                if not self._is_sensitive(path)
            }
            self._candidate = dict(filtered)
            self._version += 1
            return dict(self._candidate)

    def commit(self, session_id, version=None, confirmed=False, fail_nodes=None):
        with self._guard:
            self._expire_pending()
            self._require_lock(session_id, "running")
            if version is None:
                raise NetconfError(STALE_VERSION, "commit requires a version")
            if version != self._version:
                raise NetconfError(
                    STALE_VERSION,
                    "commit version %s does not match current version %s"
                    % (version, self._version),
                )
            if self._last_commit.get(session_id) == version:
                return {
                    "status": "ok",
                    "duplicate": True,
                    "version": version,
                }

            candidate = dict(self._candidate)
            failed = [
                path for path in (fail_nodes or []) if path in candidate
            ]
            if failed:
                errors = [
                    {
                        "error-path": path,
                        "error-tag": INVALID_VALUE,
                        "error-type": "application",
                        "error-severity": "error",
                    }
                    for path in failed
                ]
                raise NetconfError(
                    ROLLBACK_ON_ERROR,
                    "commit failed on %d node(s)" % len(errors),
                    errors=errors,
                )

            previous_running = dict(self._running)
            self._running.update(candidate)
            self._candidate = {}
            self._last_commit[session_id] = version
            result = {"status": "ok", "version": version, "duplicate": False}
            if confirmed:
                self._pending = {
                    "version": version,
                    "deadline": self._clock + self.confirmed_timeout,
                    "rollback_running": previous_running,
                    "session": session_id,
                }
                result["status"] = "pending"
                result["deadline"] = self._pending["deadline"]
            else:
                self._pending = None
            return result

    def confirm(self, session_id):
        with self._guard:
            self._expire_pending()
            if self._pending is None:
                raise NetconfError(
                    OPERATION_FAILED, "no pending confirmed commit"
                )
            pending = self._pending
            self._pending = None
            return {
                "status": "confirmed",
                "version": pending["version"],
                "session": session_id,
            }

    def pending_commit(self):
        with self._guard:
            self._expire_pending()
            return dict(self._pending) if self._pending is not None else None

    def advance(self, seconds):
        with self._guard:
            self._clock += seconds
            return self._expire_pending()

    def snapshot(self):
        with self._guard:
            self._expire_pending()
            return {
                "clock": self._clock,
                "version": self._version,
                "confirmed_timeout": self.confirmed_timeout,
                "defaults": dict(self.defaults),
                "sensitive_nodes": list(self.sensitive_nodes),
                "running": dict(self._running),
                "candidate": dict(self._candidate),
                "sessions": {
                    session_id: {"locks": sorted(session["locks"])}
                    for session_id, session in self._sessions.items()
                },
                "locks": dict(self._locks),
                "last_commit": dict(self._last_commit),
                "pending": dict(self._pending) if self._pending is not None else None,
            }

    def restore(self, snapshot):
        with self._guard:
            data = dict(snapshot or {})
            self._clock = data.get("clock", 0)
            self._version = data.get("version", 0)
            self.confirmed_timeout = data.get(
                "confirmed_timeout", self.confirmed_timeout
            )
            self.defaults = dict(data.get("defaults", {}))
            self.sensitive_nodes = tuple(
                data.get("sensitive_nodes", SENSITIVE_NODES)
            )
            self._running = dict(data.get("running", {}))
            self._candidate = dict(data.get("candidate", {}))
            self._locks = dict(data.get("locks", {}))
            self._last_commit = dict(data.get("last_commit", {}))
            self._pending = dict(data["pending"]) if data.get("pending") else None
            self._sessions = {
                session_id: {"locks": set(session.get("locks", ()))}
                for session_id, session in data.get("sessions", {}).items()
            }
            for datastore, holder in self._locks.items():
                if holder not in self._sessions:
                    self._sessions[holder] = {"locks": {datastore}}
                else:
                    self._sessions[holder]["locks"].add(datastore)
            return self
