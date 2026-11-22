"""EST certificate enrollment workflow skeleton."""

UNTRUSTED_CHAIN = "untrusted-chain"
CHALLENGE_MISMATCH = "challenge-mismatch"
SUBJECT_MISMATCH = "subject-mismatch"
KEY_MISMATCH = "key-mismatch"
CLIENT_CERT_REQUIRED = "client-cert-required"
QUOTA_EXCEEDED = "quota-exceeded"
REVOKED = "revoked"
INVALID_CSR = "invalid-csr"
UNTRUSTED_SIGNER = "untrusted-signer"


class EstError(Exception):
    def __init__(self, code, message=""):
        self.code = code
        super().__init__(code if not message else "%s: %s" % (code, message))


class EstEnrollmentWarden:
    """Device-scoped EST enrollment with pending-task persistence.

    Enrollment is idempotent per device, quota is per device rather than per
    source address, and 202-style deferred responses are retried through
    retry_pending().  Issued certificates carry only allowed key usages.
    """

    def __init__(self, ca_cert=None, trust_store=(), otps=None,
                 quota_per_device=2, allowed_key_usages=("digitalSignature",),
                 trusted_signers=("CN=CA",)):
        self.ca_cert = ca_cert or {"subject": "CN=CA", "signature_valid": True}
        self.trust_store = tuple(trust_store)
        self.otps = dict(otps or {})
        self.quota_per_device = quota_per_device
        self.allowed_key_usages = tuple(allowed_key_usages)
        self.trusted_signers = tuple(trusted_signers)

    def verify_cacerts(self, chain):
        raise NotImplementedError

    def verify_response(self, response):
        raise NotImplementedError

    def enroll(self, device_id, csr, challenge_password, client_cert=None,
               source_ip="", defer=False, now=0):
        raise NotImplementedError

    def renew(self, device_id, old_cert, csr, client_cert=None, now=0):
        raise NotImplementedError

    def revoke(self, serial):
        raise NotImplementedError

    def retry_pending(self, now=0):
        raise NotImplementedError

    def task_state(self, device_id):
        raise NotImplementedError

    def pending_count(self):
        raise NotImplementedError

    def issue_count(self, device_id):
        raise NotImplementedError

    def snapshot(self):
        raise NotImplementedError

    def restore(self, snapshot):
        raise NotImplementedError
