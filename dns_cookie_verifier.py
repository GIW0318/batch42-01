"""DNS Cookie key-versioned verifier (skeleton)."""


class DnsCookieError(Exception):
    def __init__(self, code, message=""):
        self.code = code
        super().__init__(code if not message else "%s: %s" % (code, message))


class DnsCookieVerifier:
    def __init__(self, secret=b"default-secret-0123456789abcdef",
                 cookie_ttl=300, rotation_grace=300):
        self.cookie_ttl = cookie_ttl
        self.rotation_grace = rotation_grace

    def make_server_cookie(self, client_ip, client_cookie=b"12345678", *,
                           version=None, now=0.0):
        raise NotImplementedError

    def verify(self, client_cookie=None, server_cookie=None, client_ip="", *,
               udp=True, qtype="A", now=0.0):
        raise NotImplementedError

    def rotate_secret(self, new_secret, *, now=0.0):
        raise NotImplementedError

    def nonce(self, client_ip):
        raise NotImplementedError

    def snapshot(self):
        raise NotImplementedError

    def restore(self, snapshot):
        raise NotImplementedError

    def audit_events(self):
        raise NotImplementedError
