"""OAuth authorization-code plus PKCE one-time grant gate (skeleton)."""


class OAuthError(Exception):
    def __init__(self, code, message=""):
        self.code = code
        super().__init__(code if not message else "%s: %s" % (code, message))


class GrantExchangeGate:
    def __init__(self, code_ttl=600):
        self.code_ttl = code_ttl

    def register_client(self, client_id, *, client_type="public",
                        redirect_uris=()):
        raise NotImplementedError

    def authorize(self, client_id, redirect_uri, scope, *, code_challenge=None,
                  method="S256", now=0.0):
        raise NotImplementedError

    def exchange(self, code, client_id, redirect_uri, *, code_verifier=None,
                 now=0.0):
        raise NotImplementedError

    def refresh(self, refresh_token, client_id, *, scope=None, now=0.0):
        raise NotImplementedError

    def snapshot(self):
        raise NotImplementedError

    def restore(self, snapshot):
        raise NotImplementedError

    def audit_events(self):
        raise NotImplementedError
