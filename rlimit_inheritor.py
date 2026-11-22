"""Resource-limit inheritance and enforcement skeleton."""

INVALID_LIMIT = "invalid-limit"
PERMISSION_DENIED = "permission-denied"
UNIT_LIMIT = "unit-limit"
UNKNOWN_RESOURCE = "unknown-resource"
EMFILE = "EMFILE"


class RlimitError(Exception):
    def __init__(self, code, message=""):
        self.code = code
        super().__init__(code if not message else "%s: %s" % (code, message))


class RlimitInheritor:
    """Soft/hard rlimit pairs inherited across fork and exec.

    Soft must not exceed hard.  Raising a hard limit needs privilege or a
    pre-existing larger hard limit.  Unit limits act as a ceiling.  NOFILE
    enforcement counts open descriptors and raises EMFILE on exhaustion.
    CPU soft-limit signals get a grace period before the process is killed.
    """

    def __init__(self, privileged=False, unit_limits=None, max_values=None,
                 cpu_grace=5):
        self.privileged = privileged
        self.unit_limits = dict(unit_limits or {})
        self.max_values = dict(max_values or {"NOFILE": 2 ** 40,
                                             "NPROC": 2 ** 20,
                                             "CPU": 2 ** 40})
        self.cpu_grace = cpu_grace
        self._clock = 0

    def set_limit(self, pid, resource, soft, hard):
        raise NotImplementedError

    def get_limit(self, pid, resource):
        raise NotImplementedError

    def fork(self, parent_id, child_id):
        raise NotImplementedError

    def exec_process(self, pid, new_pid):
        raise NotImplementedError

    def open_fd(self, pid, count=1):
        raise NotImplementedError

    def close_fd(self, pid, fd):
        raise NotImplementedError

    def open_fd_count(self, pid):
        raise NotImplementedError

    def set_unit_limit(self, resource, soft, hard):
        raise NotImplementedError

    def cpu_signal(self, pid):
        raise NotImplementedError

    def is_killed(self, pid):
        raise NotImplementedError

    def register_process(self, pid, uid, namespace="host"):
        raise NotImplementedError

    def process_count(self, uid, namespace="host"):
        raise NotImplementedError

    def advance(self, seconds):
        raise NotImplementedError

    def snapshot(self):
        raise NotImplementedError

    def restore(self, snapshot):
        raise NotImplementedError
