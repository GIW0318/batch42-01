"""Core-dump filtering, naming and handler policy (skeleton)."""


class CoreDumpError(Exception):
    def __init__(self, code, message=""):
        self.code = code
        super().__init__(code if not message else "%s: %s" % (code, message))


class CoreDumpPolicy:
    def __init__(self, core_dir="/var/crash", max_bytes=1 << 30,
                 max_concurrent=4, sensitive_paths=()):
        self.core_dir = core_dir
        self.max_bytes = max_bytes
        self.max_concurrent = max_concurrent
        self.sensitive_paths = list(sensitive_paths)

    def decide(self, process):
        raise NotImplementedError

    def dump(self, process, memory, *, now=0.0, realpath=None):
        raise NotImplementedError

    def read_dump(self, path):
        raise NotImplementedError

    def handle_failure(self, process):
        raise NotImplementedError

    def set_handler(self, name):
        raise NotImplementedError

    def handlers(self):
        raise NotImplementedError

    def cleanup(self, *, now=0.0, max_age=86400.0):
        raise NotImplementedError

    def audit_events(self):
        raise NotImplementedError
