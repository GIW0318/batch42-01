"""Resource-limit inheritance and enforcement."""

import threading

INVALID_LIMIT = "invalid-limit"
PERMISSION_DENIED = "permission-denied"
UNIT_LIMIT = "unit-limit"
UNKNOWN_RESOURCE = "unknown-resource"
EMFILE = "EMFILE"

INT64_MAX = 2 ** 63 - 1

DEFAULT_SOFT = 1024
DEFAULT_HARD = 4096


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
        self.unit_limits = {resource: dict(pair)
                            for resource, pair in (unit_limits or {}).items()}
        self.max_values = dict(max_values or {"NOFILE": 2 ** 40,
                                             "NPROC": 2 ** 20,
                                             "CPU": 2 ** 40})
        self.cpu_grace = cpu_grace
        self._clock = 0
        self._lock = threading.RLock()
        self._limits = {}
        self._fds = {}
        self._processes = {}
        self._cpu_signals = {}
        self._killed = set()

    def _check_resource(self, resource):
        if resource not in self.max_values:
            raise RlimitError(UNKNOWN_RESOURCE, resource)

    def _check_value(self, resource, value):
        if isinstance(value, bool) or not isinstance(value, int):
            raise RlimitError(INVALID_LIMIT, "not an integer: %r" % (value,))
        if value < 0 or value > INT64_MAX:
            raise RlimitError(INVALID_LIMIT,
                              "out of 64-bit range: %r" % (value,))
        if value > self.max_values[resource]:
            raise RlimitError(INVALID_LIMIT,
                              "exceeds max for %s: %r" % (resource, value))

    def _ensure_process(self, pid):
        if pid not in self._limits:
            self._limits[pid] = {
                resource: {"soft": DEFAULT_SOFT, "hard": DEFAULT_HARD}
                for resource in self.max_values
            }
            self._fds[pid] = set()

    def set_limit(self, pid, resource, soft, hard):
        with self._lock:
            self._check_resource(resource)
            self._check_value(resource, soft)
            self._check_value(resource, hard)
            if soft > hard:
                raise RlimitError(INVALID_LIMIT, "soft exceeds hard")
            self._ensure_process(pid)
            current = self._limits[pid][resource]
            ceiling = self.unit_limits.get(resource)
            if ceiling is not None:
                if soft > ceiling["soft"] or hard > ceiling["hard"]:
                    raise RlimitError(UNIT_LIMIT,
                                      "exceeds unit limit for %s" % resource)
            if hard > current["hard"] and not self.privileged:
                raise RlimitError(PERMISSION_DENIED,
                                  "raising hard limit needs privilege")
            self._limits[pid][resource] = {"soft": soft, "hard": hard}

    def get_limit(self, pid, resource):
        with self._lock:
            self._check_resource(resource)
            self._ensure_process(pid)
            return dict(self._limits[pid][resource])

    def fork(self, parent_id, child_id):
        with self._lock:
            self._ensure_process(parent_id)
            self._limits[child_id] = {
                resource: dict(pair)
                for resource, pair in self._limits[parent_id].items()
            }
            self._fds[child_id] = set(self._fds[parent_id])
            if parent_id in self._processes:
                self._processes[child_id] = dict(self._processes[parent_id])

    def exec_process(self, pid, new_pid):
        with self._lock:
            self._ensure_process(pid)
            self._limits[new_pid] = self._limits.pop(pid)
            self._fds[new_pid] = self._fds.pop(pid)
            if pid in self._processes:
                self._processes[new_pid] = self._processes.pop(pid)
            if pid in self._cpu_signals:
                self._cpu_signals[new_pid] = self._cpu_signals.pop(pid)
            if pid in self._killed:
                self._killed.discard(pid)
                self._killed.add(new_pid)

    def open_fd(self, pid, count=1):
        with self._lock:
            self._ensure_process(pid)
            soft = self._limits[pid]["NOFILE"]["soft"]
            fds = self._fds[pid]
            if len(fds) + count > soft:
                raise RlimitError(EMFILE, "too many open files")
            opened = []
            candidate = 0
            while len(opened) < count:
                if candidate not in fds:
                    fds.add(candidate)
                    opened.append(candidate)
                candidate += 1
            return opened

    def close_fd(self, pid, fd):
        with self._lock:
            self._ensure_process(pid)
            self._fds[pid].discard(fd)

    def open_fd_count(self, pid):
        with self._lock:
            self._ensure_process(pid)
            return len(self._fds[pid])

    def set_unit_limit(self, resource, soft, hard):
        with self._lock:
            self._check_resource(resource)
            self._check_value(resource, soft)
            self._check_value(resource, hard)
            if soft > hard:
                raise RlimitError(INVALID_LIMIT, "soft exceeds hard")
            self.unit_limits[resource] = {"soft": soft, "hard": hard}

    def cpu_signal(self, pid):
        with self._lock:
            self._ensure_process(pid)
            self._cpu_signals[pid] = self._clock
            return {"signal": "SIGXCPU", "pid": pid, "grace": self.cpu_grace}

    def is_killed(self, pid):
        with self._lock:
            if pid in self._killed:
                return True
            signaled_at = self._cpu_signals.get(pid)
            if signaled_at is None:
                return False
            if self._clock - signaled_at > self.cpu_grace:
                self._killed.add(pid)
                return True
            return False

    def register_process(self, pid, uid, namespace="host"):
        with self._lock:
            self._processes[pid] = {"uid": uid, "namespace": namespace}

    def process_count(self, uid, namespace="host"):
        with self._lock:
            return sum(
                1 for info in self._processes.values()
                if info["uid"] == uid and info["namespace"] == namespace
            )

    def advance(self, seconds):
        with self._lock:
            self._clock += seconds

    def snapshot(self):
        with self._lock:
            return {
                "clock": self._clock,
                "limits": {
                    pid: {resource: dict(pair)
                          for resource, pair in resources.items()}
                    for pid, resources in self._limits.items()
                },
                "fds": {pid: sorted(fds) for pid, fds in self._fds.items()},
                "processes": {pid: dict(info)
                              for pid, info in self._processes.items()},
                "cpu_signals": dict(self._cpu_signals),
                "killed": sorted(self._killed),
                "unit_limits": {resource: dict(pair)
                                for resource, pair in self.unit_limits.items()},
            }

    def restore(self, snapshot):
        with self._lock:
            self._clock = snapshot.get("clock", 0)
            self._limits = {}
            for pid, resources in snapshot.get("limits", {}).items():
                pairs = {}
                for resource, pair in resources.items():
                    self._check_resource(resource)
                    soft = pair["soft"]
                    hard = pair["hard"]
                    self._check_value(resource, soft)
                    self._check_value(resource, hard)
                    if soft > hard:
                        raise RlimitError(INVALID_LIMIT, "soft exceeds hard")
                    pairs[resource] = {"soft": soft, "hard": hard}
                self._limits[pid] = pairs
            self._fds = {pid: set(fds)
                         for pid, fds in snapshot.get("fds", {}).items()}
            for pid in self._limits:
                self._fds.setdefault(pid, set())
            self._processes = {pid: dict(info) for pid, info
                               in snapshot.get("processes", {}).items()}
            self._cpu_signals = dict(snapshot.get("cpu_signals", {}))
            self._killed = set(snapshot.get("killed", []))
            self.unit_limits = {resource: dict(pair) for resource, pair
                                in snapshot.get("unit_limits", {}).items()}
