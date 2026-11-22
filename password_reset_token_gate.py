"""Password-reset token lifecycle and session revocation gate (skeleton)."""


class ResetTokenError(Exception):
    def __init__(self, code, message=""):
        self.code = code
        super().__init__(code if not message else "%s: %s" % (code, message))


class ResetTokenGate:
    def __init__(self, token_ttl=1800, max_requests=3, request_window=3600):
        self.token_ttl = token_ttl
        self.max_requests = max_requests
        self.request_window = request_window

    def add_account(self, account, email):
        raise NotImplementedError

    def delete_account(self, account):
        raise NotImplementedError

    def add_session(self, account):
        raise NotImplementedError

    def add_remember_token(self, account):
        raise NotImplementedError

    def active_sessions(self, account):
        raise NotImplementedError

    def active_remember_tokens(self, account):
        raise NotImplementedError

    def pending_tokens(self, account):
        raise NotImplementedError

    def request_reset(self, account, *, email=None, ip="0.0.0.0", now=0.0,
                      mailer=None):
        raise NotImplementedError

    def reset(self, token, new_password, *, now=0.0, ip=""):
        raise NotImplementedError

    def admin_reset(self, actor, account, *, confirm=False, now=0.0, ip=""):
        raise NotImplementedError

    def snapshot(self):
        raise NotImplementedError

    def restore(self, snapshot):
        raise NotImplementedError

    def audit_events(self):
        raise NotImplementedError
