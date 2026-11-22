"""Recovery-code generation, storage and consumption skeleton."""

import hashlib
import hmac
import secrets
import threading
import time

ACCOUNT_DISABLED = "account-disabled"
INVALID_CODE = "invalid-code"
LOW_CODE_SPACE = "low-code-space"


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
        if isinstance(salt, str):
            salt = salt.encode("utf-8")
        self.salt = salt
        self.code_count = code_count
        self.code_length = code_length
        self.alphabet = alphabet
        self.low_threshold = low_threshold
        self._lock = threading.Lock()
        self._codes = {}
        self._account_states = {}
        self._failures = {}
        self._consumption_log = []
        self._consumption_index = {}

    def generate(self, account):
        codes = []
        seen = set()
        while len(codes) < self.code_count:
            code = "".join(secrets.choice(self.alphabet)
                           for _ in range(self.code_length))
            if code in seen:
                continue
            seen.add(code)
            codes.append(code)
        with self._lock:
            self._codes.setdefault(account, set()).update(
                self.digest_code(account, code) for code in codes)
        return codes

    def normalize_code(self, code):
        return "".join(ch for ch in str(code).upper()
                       if ch not in " -")

    def digest_code(self, account, code):
        normalized = self.normalize_code(code)
        message = ("%s:%s" % (account, normalized)).encode("utf-8")
        return hmac.new(self.salt, message, hashlib.sha256).hexdigest()

    def verify(self, account, code):
        digest = self.digest_code(account, code)
        with self._lock:
            stored = list(self._codes.get(account, ()))
        matched = False
        for candidate in stored:
            if hmac.compare_digest(digest, candidate):
                matched = True
        return matched

    def consume(self, account, code):
        digest = self.digest_code(account, code)
        with self._lock:
            state = self._account_states.get(account)
            if state is not None and state != "active":
                return False
            stored = self._codes.get(account, set())
            matched = None
            for candidate in stored:
                if hmac.compare_digest(digest, candidate):
                    matched = candidate
            if matched is None:
                return False
            stored.discard(matched)
            self._record_consumption(account, matched)
            return True

    def regenerate(self, account):
        codes = []
        seen = set()
        while len(codes) < self.code_count:
            code = "".join(secrets.choice(self.alphabet)
                           for _ in range(self.code_length))
            if code in seen:
                continue
            seen.add(code)
            codes.append(code)
        with self._lock:
            self._codes[account] = set(
                self.digest_code(account, code) for code in codes)
        return codes

    def remaining(self, account):
        with self._lock:
            return len(self._codes.get(account, ()))

    def set_account_state(self, account, state):
        with self._lock:
            self._account_states[account] = state

    def record_failure(self, account, kind="recovery"):
        with self._lock:
            counters = self._failures.setdefault(account, {})
            counters[kind] = counters.get(kind, 0) + 1

    def failed_attempts(self, account, kind="recovery"):
        with self._lock:
            return self._failures.get(account, {}).get(kind, 0)

    def warnings(self, account):
        with self._lock:
            remaining = len(self._codes.get(account, ()))
        if remaining <= self.low_threshold:
            return [LOW_CODE_SPACE]
        return []

    def snapshot(self):
        with self._lock:
            return {
                "codes": {account: sorted(digests)
                          for account, digests in self._codes.items()},
                "account_states": dict(self._account_states),
                "failures": {account: dict(counters)
                             for account, counters in self._failures.items()},
                "consumption_log": [dict(entry)
                                    for entry in self._consumption_log],
            }

    def restore(self, snapshot):
        with self._lock:
            self._codes = {account: set(digests)
                           for account, digests
                           in snapshot.get("codes", {}).items()}
            self._account_states = dict(snapshot.get("account_states", {}))
            self._failures = {account: dict(counters)
                              for account, counters
                              in snapshot.get("failures", {}).items()}
            self._consumption_log = [dict(entry) for entry
                                     in snapshot.get("consumption_log", [])]
            self._consumption_index = {}
            for entry in self._consumption_log:
                self._consumption_index.setdefault(
                    entry["account"], []).append(entry)

    def _record_consumption(self, account, digest):
        entry = {
            "account": account,
            "digest": digest,
            "consumed_at": time.time(),
        }
        self._consumption_log.append(entry)
        self._consumption_index.setdefault(account, []).append(entry)

    def consumption_records(self, account=None):
        with self._lock:
            if account is None:
                return [dict(entry) for entry in self._consumption_log]
            return [dict(entry)
                    for entry in self._consumption_index.get(account, [])]
