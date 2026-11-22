"""SASL mechanism negotiation and multi-round state skeleton."""

UNKNOWN_MECHANISM = "unknown-mechanism"
NO_MECHANISM = "no-mechanism"
CERTIFICATE_REQUIRED = "certificate-required"
INVALID_AUTHZID = "invalid-authzid"
INVALID_AUTHCID = "invalid-authcid"
AUTHZID_NOT_ALLOWED = "authzid-not-allowed"
RESPONSE_TOO_LARGE = "response-too-large"
CHANNEL_BINDING_REQUIRED = "channel-binding-required"
NO_SESSION = "no-session"
SESSION_BUSY = "session-busy"
ACCOUNT_LOCKED = "account-locked"


class SaslError(Exception):
    def __init__(self, code, message=""):
        self.code = code
        super().__init__(code if not message else "%s: %s" % (code, message))


class SaslMechanismNegotiator:
    """Chooses the strongest mutually supported SASL mechanism.

    Mechanism names are case-insensitive.  EXTERNAL needs a client
    certificate and an explicit authorization identity.  Multi-round state is
    per connection and abort clears it.  Failure counters are per account.
    """

    STRENGTH = {
        "EXTERNAL": 5,
        "GSSAPI": 4,
        "SCRAM-SHA-256": 3,
        "PLAIN": 1,
    }

    def __init__(self, mechanisms=("EXTERNAL", "GSSAPI", "SCRAM-SHA-256", "PLAIN"),
                 max_initial=1024, require_channel_binding=False,
                 proxy_allowed=(), max_failures=3):
        self.mechanisms = tuple(name.upper() for name in mechanisms)
        self.max_initial = max_initial
        self.require_channel_binding = require_channel_binding
        self.proxy_allowed = tuple(proxy_allowed)
        self.max_failures = max_failures

    def negotiate(self, conn_id, client_mechanisms, has_client_cert=False,
                  channel_binding=False):
        raise NotImplementedError

    def start(self, conn_id, mechanism, initial_response, authcid=None,
              authzid=None, has_client_cert=False):
        raise NotImplementedError

    def step(self, conn_id, challenge_response):
        raise NotImplementedError

    def abort(self, conn_id):
        raise NotImplementedError

    def bind(self, conn_id, authcid, authzid=None):
        raise NotImplementedError

    def connection_state(self, conn_id):
        raise NotImplementedError

    def security_layer(self, conn_id):
        raise NotImplementedError

    def claims_encrypted(self, conn_id):
        raise NotImplementedError

    def record_failure(self, account):
        raise NotImplementedError

    def attempt_allowed(self, account):
        raise NotImplementedError

    def audit_log(self):
        raise NotImplementedError
