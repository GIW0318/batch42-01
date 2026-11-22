"""One-time magic link issuance and redemption skeleton."""

INVALID_TOKEN = "invalid-token"
INVALID_REDIRECT = "invalid-redirect"
RATE_LIMITED = "rate-limited"


class MagicLinkError(Exception):
    def __init__(self, code, message=""):
        self.code = code
        super().__init__(code if not message else "%s: %s" % (code, message))


class MagicLinkRedeemer:
    """Salted token digests, one-time redemption and email-bound links.

    Tokens are returned in clear only from issue()/resend(); snapshots keep
    digests only.  request_id makes a repeated redemption idempotent while a
    different request id is rejected.  Rate limiting is per normalized email,
    independent of the source address.
    """

    def __init__(self, secret, ttl=900, redirect_allowlist=(),
                 max_requests_per_hour=5):
        self.secret = secret
        self.ttl = ttl
        self.redirect_allowlist = tuple(redirect_allowlist)
        self.max_requests_per_hour = max_requests_per_hour

    def normalize_email(self, email):
        raise NotImplementedError

    def issue(self, email, redirect_url=None, now=0, source_ip=""):
        raise NotImplementedError

    def resend(self, email, redirect_url=None, now=0, source_ip=""):
        raise NotImplementedError

    def preview(self, token, now=0):
        raise NotImplementedError

    def redeem(self, token, email, redirect_url=None, now=0, request_id=None):
        raise NotImplementedError

    def request_count(self, email):
        raise NotImplementedError

    def snapshot(self):
        raise NotImplementedError

    def restore(self, snapshot):
        raise NotImplementedError
