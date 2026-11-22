"""OAuth device-code flow gate keyed by user code, device code and client (skeleton)."""


class DeviceCodeError(Exception):
    def __init__(self, code, message=""):
        self.code = code
        super().__init__(code if not message else "%s: %s" % (code, message))


class DeviceCodeGate:
    def __init__(self, interval=5, expires_in=600):
        self.interval = interval
        self.expires_in = expires_in

    def register_client(self, client_id, *, name="app", scopes=()):
        raise NotImplementedError

    def start(self, client_id, scope):
        raise NotImplementedError

    def describe(self, user_code):
        raise NotImplementedError

    def approve(self, user_code, user_id, *, scope=None, now=0.0):
        raise NotImplementedError

    def deny(self, user_code, user_id, now=0.0):
        raise NotImplementedError

    def poll(self, device_code, client_id, *, now=0.0, client_ip="0.0.0.0"):
        raise NotImplementedError

    def snapshot(self):
        raise NotImplementedError

    def restore(self, snapshot):
        raise NotImplementedError

    def audit_events(self):
        raise NotImplementedError
