"""EST certificate enrollment workflow skeleton."""

import copy
import threading

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
        self._lock = threading.RLock()
        self._serial_counter = 0
        self._issued = {}
        self._idempotent = {}
        self._revoked = set()
        self._pending = {}

    def _next_serial(self):
        self._serial_counter += 1
        return "serial-%d" % self._serial_counter

    @staticmethod
    def _validate_csr(csr):
        if not isinstance(csr, dict):
            raise EstError(INVALID_CSR, "csr must be a mapping")
        if not csr.get("subject") or not csr.get("public_key"):
            raise EstError(INVALID_CSR, "csr requires subject and public_key")

    @staticmethod
    def _idem_key(device_id, csr):
        return (device_id, csr.get("subject"), csr.get("public_key"),
                tuple(csr.get("key_usage") or ()))

    def _filter_key_usage(self, csr):
        requested = csr.get("key_usage")
        if not requested:
            return list(self.allowed_key_usages)
        allowed = set(self.allowed_key_usages)
        return [usage for usage in requested if usage in allowed]

    def _issue(self, device_id, csr):
        cert = {
            "serial": self._next_serial(),
            "subject": csr["subject"],
            "public_key": csr["public_key"],
            "key_usage": self._filter_key_usage(csr),
            "issuer": self.ca_cert.get("subject"),
            "status": "issued",
        }
        self._issued.setdefault(device_id, []).append(cert)
        self._idempotent[self._idem_key(device_id, csr)] = cert
        return cert

    def verify_cacerts(self, chain):
        subjects = {cert.get("subject") for cert in chain}
        for cert in chain:
            if not cert.get("signature_valid"):
                raise EstError(UNTRUSTED_CHAIN, "invalid signature in chain")
            issuer = cert.get("issuer")
            if issuer not in self.trust_store and issuer not in subjects:
                raise EstError(UNTRUSTED_CHAIN, "issuer not trusted")
        return True

    def verify_response(self, response):
        if (response.get("signer") not in self.trusted_signers
                or not response.get("signature_valid")):
            raise EstError(UNTRUSTED_SIGNER, "untrusted pkcs7 signer")
        return True

    def enroll(self, device_id, csr, challenge_password, client_cert=None,
               source_ip="", defer=False, now=0):
        with self._lock:
            if client_cert is None:
                raise EstError(CLIENT_CERT_REQUIRED,
                               "client certificate required")
            self._validate_csr(csr)
            otp = self.otps.get(device_id)
            if (otp is None or challenge_password != otp
                    or csr.get("challenge_password") != otp):
                raise EstError(CHALLENGE_MISMATCH,
                               "challenge password does not match otp")
            if self.issue_count(device_id) >= self.quota_per_device:
                raise EstError(QUOTA_EXCEEDED, "device quota exceeded")
            existing = self._idempotent.get(self._idem_key(device_id, csr))
            if existing is not None:
                if existing["serial"] in self._revoked:
                    raise EstError(REVOKED, "certificate revoked")
                return dict(existing)
            if device_id in self._pending:
                return dict(self._pending[device_id]["response"])
            if defer:
                response = {"status": "pending", "retry_after": 5}
                self._pending[device_id] = {
                    "device_id": device_id,
                    "csr": copy.deepcopy(csr),
                    "ready_at": now + response["retry_after"],
                    "response": response,
                }
                return dict(response)
            return self._issue(device_id, csr)

    def renew(self, device_id, old_cert, csr, client_cert=None, now=0):
        with self._lock:
            if client_cert is None:
                raise EstError(CLIENT_CERT_REQUIRED,
                               "client certificate required")
            self._validate_csr(csr)
            if old_cert.get("serial") in self._revoked:
                raise EstError(REVOKED, "certificate revoked")
            if csr["subject"] != old_cert.get("subject"):
                raise EstError(SUBJECT_MISMATCH,
                               "renewal subject differs from old certificate")
            if csr["public_key"] != old_cert.get("public_key"):
                raise EstError(KEY_MISMATCH,
                               "renewal key differs from old certificate")
            cert = self._issue(device_id, csr)
            self._revoked.add(old_cert.get("serial"))
            return cert

    def revoke(self, serial):
        with self._lock:
            self._revoked.add(serial)

    def retry_pending(self, now=0):
        issued = []
        with self._lock:
            ready = [device_id for device_id, task in self._pending.items()
                     if task["ready_at"] <= now]
            for device_id in ready:
                task = self._pending.pop(device_id)
                if self.issue_count(device_id) >= self.quota_per_device:
                    continue
                cert = self._issue(device_id, task["csr"])
                issued.append(cert)
        return issued

    def task_state(self, device_id):
        with self._lock:
            if device_id in self._pending:
                return "pending"
            if self._issued.get(device_id):
                return "issued"
            return None

    def pending_count(self):
        with self._lock:
            return len(self._pending)

    def issue_count(self, device_id):
        with self._lock:
            return len(self._issued.get(device_id, ()))

    def snapshot(self):
        with self._lock:
            return {
                "serial_counter": self._serial_counter,
                "issued": copy.deepcopy(self._issued),
                "idempotent": [[list(key), copy.deepcopy(cert)]
                               for key, cert in self._idempotent.items()],
                "revoked": sorted(s for s in self._revoked if s is not None),
                "pending": copy.deepcopy(self._pending),
            }

    def restore(self, snapshot):
        with self._lock:
            self._serial_counter = snapshot.get("serial_counter", 0)
            self._issued = copy.deepcopy(snapshot.get("issued", {}))
            self._idempotent = {
                tuple(key): copy.deepcopy(cert)
                for key, cert in snapshot.get("idempotent", [])
            }
            self._revoked = set(snapshot.get("revoked", ()))
            self._pending = copy.deepcopy(snapshot.get("pending", {}))
