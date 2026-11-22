"""NETCONF candidate/running lock, commit and rollback skeleton."""

LOCK_DENIED = "lock-denied"
STALE_VERSION = "stale-version"
ROLLBACK_ON_ERROR = "rollback-on-error"
INVALID_VALUE = "invalid-value"
SENSITIVE_NODES = ("private-key", "community")


class NetconfError(Exception):
    def __init__(self, code, message="", errors=None):
        self.code = code
        self.errors = list(errors or [])
        super().__init__(code if not message else "%s: %s" % (code, message))


class NetconfCandidateLock:
    """Session-scoped NETCONF edit lock with candidate/running commit.

    A lock on "running" protects both running and candidate.  A lock on
    "candidate" protects only the candidate datastore.  Commits compare the
    caller-supplied candidate version against the current version, apply all
    nodes or none, and a confirmed commit rolls back when its persisted
    deadline passes without confirmation.
    """

    def __init__(self, confirmed_timeout=600, defaults=None, initial_running=None,
                 sensitive_nodes=None):
        self.confirmed_timeout = confirmed_timeout
        self.defaults = dict(defaults or {})
        self.initial_running = dict(initial_running or {})
        self.sensitive_nodes = tuple(sensitive_nodes or SENSITIVE_NODES)
        self._clock = 0

    def open_session(self, session_id):
        raise NotImplementedError

    def close_session(self, session_id):
        raise NotImplementedError

    def lock(self, session_id, datastore="running"):
        raise NotImplementedError

    def unlock(self, session_id, datastore="running"):
        raise NotImplementedError

    def edit_candidate(self, session_id, changes):
        raise NotImplementedError

    def get_candidate(self, session_id=None):
        raise NotImplementedError

    def get_running(self, session_id=None):
        raise NotImplementedError

    def discard_changes(self, session_id, expected_version=None):
        raise NotImplementedError

    def copy_config(self, session_id, source="running", target="candidate"):
        raise NotImplementedError

    def commit(self, session_id, version=None, confirmed=False, fail_nodes=None):
        raise NotImplementedError

    def confirm(self, session_id):
        raise NotImplementedError

    def pending_commit(self):
        raise NotImplementedError

    def advance(self, seconds):
        raise NotImplementedError

    def snapshot(self):
        raise NotImplementedError

    def restore(self, snapshot):
        raise NotImplementedError
