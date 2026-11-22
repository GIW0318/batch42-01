"""PPPoE access registry keyed by MAC plus session id (skeleton)."""


class PppoeError(Exception):
    def __init__(self, code, message=""):
        self.code = code
        super().__init__(code if not message else "%s: %s" % (code, message))


class PppoeAccessRegistry:
    def __init__(self, max_sessions_per_account=1, auth_failure_limit=3,
                 idle_timeout=300, discovery_pps=10, session_fee=1.0):
        self.max_sessions_per_account = max_sessions_per_account
        self.auth_failure_limit = auth_failure_limit
        self.idle_timeout = idle_timeout
        self.discovery_pps = discovery_pps
        self.session_fee = session_fee

    def dial(self, mac, session_id, account, service="default", now=0.0):
        raise NotImplementedError

    def disconnect(self, mac, session_id, epoch):
        raise NotImplementedError

    def account_sessions(self, account):
        raise NotImplementedError

    def accounting_total(self, account):
        raise NotImplementedError

    def authenticate(self, mac, session_id, *, ok, started_at, now, timeout=30.0):
        raise NotImplementedError

    def record_traffic(self, mac, octets, now=0.0):
        raise NotImplementedError

    def idle_check(self, now):
        raise NotImplementedError

    def set_mtu(self, mac, requested):
        raise NotImplementedError

    def open_handles(self):
        raise NotImplementedError

    def accept_discovery(self, mac, now=0.0):
        raise NotImplementedError

    def normalize_service(self, name):
        raise NotImplementedError

    def bulk_disconnect(self, macs, *, fail_stop=(), now=0.0):
        raise NotImplementedError

    def retry_accounting(self):
        raise NotImplementedError

    def snapshot(self):
        raise NotImplementedError

    def restore(self, snapshot):
        raise NotImplementedError

    def audit_events(self):
        raise NotImplementedError
