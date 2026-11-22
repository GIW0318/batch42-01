"""PASETO claim verification skeleton."""

INVALID_TOKEN = "invalid-token"
TOKEN_REJECTED = "token rejected"


class PasetoError(Exception):
    def __init__(self, code=INVALID_TOKEN, message=""):
        self.code = code
        super().__init__(code if not message else "%s: %s" % (code, message))


class PasetoClaimVerifier:
    """Verifies tokens shaped as version.purpose.payload.footer.signature.

    payload, footer and signature are base64url without padding.  The
    signature is HMAC-SHA256 over
    "version.purpose.payload.footer.implicit" using the trusted key for the
    footer's kid.  exp and nbf are numeric seconds.  Nonces are single-use
    per tenant.  All failures raise PasetoError(INVALID_TOKEN, TOKEN_REJECTED).
    """

    def __init__(self, trusted_keys=None, tenant_skew=None,
                 max_token_bytes=4096, version="v4", purpose="public"):
        self.trusted_keys = dict(trusted_keys or {})
        self.tenant_skew = dict(tenant_skew or {})
        self.max_token_bytes = max_token_bytes
        self.version = version
        self.purpose = purpose

    def verify(self, token, audience=None, implicit="", now=0, tenant=None):
        raise NotImplementedError
