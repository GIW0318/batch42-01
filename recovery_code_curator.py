"""Recovery-code generation, storage and consumption skeleton."""

ACCOUNT_DISABLED = "account-disabled"
INVALID_CODE = "invalid-code"


class RecoveryCodeError(Exception):
    def __init__(self, code, message=""):
        self.code = code
        super().__init__(code if not message else "%s: %s" % (code, message))


class RecoveryCodeCurator:
    """Salted recovery-code digests with one-time consumption.

    Generated codes are returned in clear only once; stored records contain
    per-account salted digests.  Inputs are upper-cased with spaces and
    hyphens removed before comparison.  Failure counters are kept separately
    for recovery codes and dynamic one-time passwords.
    """

    def __init__(self, salt, code_count=10, code_length=10,
                 alphabet="ABCDEFGHJKLMNPQRSTUVWXYZ23456789",
                 low_threshold=2):
        self.salt = salt
        self.code_count = code_count
        self.code_length = code_length
        self.alphabet = alphabet
        self.low_threshold = low_threshold

    def generate(self, account):
        raise NotImplementedError

    def normalize_code(self, code):
        raise NotImplementedError

    def digest_code(self, account, code):
        raise NotImplementedError

    def verify(self, account, code):
        raise NotImplementedError

    def consume(self, account, code):
        raise NotImplementedError

    def regenerate(self, account):
        raise NotImplementedError

    def remaining(self, account):
        raise NotImplementedError

    def set_account_state(self, account, state):
        raise NotImplementedError

    def record_failure(self, account, kind="recovery"):
        raise NotImplementedError

    def failed_attempts(self, account, kind="recovery"):
        raise NotImplementedError

    def warnings(self, account):
        raise NotImplementedError

    def snapshot(self):
        raise NotImplementedError

    def restore(self, snapshot):
        raise NotImplementedError
