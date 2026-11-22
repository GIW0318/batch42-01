"""Entropy source health, seeding and consumer isolation monitor (skeleton)."""


class EntropyError(Exception):
    def __init__(self, code, message=""):
        self.code = code
        super().__init__(code if not message else "%s: %s" % (code, message))


class EntropyHealthMonitor:
    def __init__(self, pool_size=256, budget_bps=1000):
        self.pool_size = pool_size
        self.budget_bps = budget_bps

    def add_source(self, name, *, kind="software", healthy=True, weight=1.0):
        raise NotImplementedError

    def set_source_health(self, name, healthy):
        raise NotImplementedError

    def estimate(self):
        raise NotImplementedError

    def ready(self):
        raise NotImplementedError

    def status(self):
        raise NotImplementedError

    def source_shares(self):
        raise NotImplementedError

    def read(self, n, *, blocking=False, caller_ns="host", now=0.0):
        raise NotImplementedError

    def drain(self):
        raise NotImplementedError

    def seed(self, caller_ns="host"):
        raise NotImplementedError

    def restore_seed(self, seed):
        raise NotImplementedError

    def vm_restore(self, snapshot):
        raise NotImplementedError

    def snapshot(self):
        raise NotImplementedError

    def close(self):
        raise NotImplementedError

    def audit_events(self):
        raise NotImplementedError
