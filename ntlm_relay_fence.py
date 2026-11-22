"""NTLM relay fencing skeleton."""

MIC_REQUIRED = "mic-required"
CBT_MISMATCH = "cbt-mismatch"
SPN_MISMATCH = "spn-mismatch"
NTLMV1_REJECTED = "ntlmv1-rejected"
CHALLENGE_REUSE = "challenge-reuse"
ANONYMOUS_REJECTED = "anonymous-rejected"
GUEST_REJECTED = "guest-rejected"
CROSS_DOMAIN = "cross-domain-referral"
CONNECTION_CLOSED = "connection-closed"
UNKNOWN_CONNECTION = "unknown-connection"


class NtlmError(Exception):
    def __init__(self, code, message=""):
        self.code = code
        super().__init__(code if not message else "%s: %s" % (code, message))


class NtlmRelayFence:
    """Requires MIC and channel binding together before accepting NTLM.

    A challenge is issued per connection, tied to the connection's channel
    binding and target SPN, and consumed exactly once.  Failures close the
    connection and are audited with source address and target SPN.
    """

    def __init__(self, allowed_domains=("CORP",), require_mic=True,
                 require_cbt=True):
        self.allowed_domains = tuple(allowed_domains)
        self.require_mic = require_mic
        self.require_cbt = require_cbt

    def begin(self, conn_id, source_ip, target_spn, channel_binding):
        raise NotImplementedError

    def authenticate(self, conn_id, message):
        raise NotImplementedError

    def close(self, conn_id):
        raise NotImplementedError

    def connection_state(self, conn_id):
        raise NotImplementedError

    def audit_log(self):
        raise NotImplementedError
