"""OpenID4VP presentation mapping and validation skeleton."""

INVALID_PRESENTATION = "invalid-presentation"
PRESENTATION_REJECTED = "presentation rejected"


class VpError(Exception):
    def __init__(self, code=INVALID_PRESENTATION, message=""):
        self.code = code
        super().__init__(code if not message else "%s: %s" % (code, message))


class Openid4vpPresentationMapper:
    """Cross-checks request, submission and credential.

    descriptor_map must cover exactly the request input descriptors, nonce
    is one-time per session, audience and credential format must match, and
    revocation lookup failure denies.  All failures surface as one uniform
    error.
    """

    def __init__(self, audience="verifier.example", require_kb=True,
                 revoked=(), revocation_available=True):
        self.audience = audience
        self.require_kb = require_kb
        self.revoked = set(revoked)
        self.revocation_available = revocation_available

    def create_request(self, session_id, input_descriptors, nonce=None):
        raise NotImplementedError

    def submit(self, session_id, request, presentation, now=0):
        raise NotImplementedError

    def decode_direct_post(self, token, key):
        raise NotImplementedError
