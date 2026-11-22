"""PASETO claim verification skeleton."""

import base64
import binascii
import hashlib
import hmac
import json
import threading

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
        self._used_nonces = set()
        self._nonce_lock = threading.Lock()

    def verify(self, token, audience=None, implicit="", now=0, tenant=None):
        try:
            return self._verify(token, audience=audience, implicit=implicit,
                                now=now, tenant=tenant)
        except PasetoError:
            raise
        except Exception:
            raise PasetoError(INVALID_TOKEN, TOKEN_REJECTED)

    def _reject(self):
        raise PasetoError(INVALID_TOKEN, TOKEN_REJECTED)

    def _b64decode(self, segment):
        padding = "=" * (-len(segment) % 4)
        return base64.urlsafe_b64decode(segment + padding)

    def _verify(self, token, audience=None, implicit="", now=0, tenant=None):
        if isinstance(token, str):
            token_bytes = token.encode("utf-8")
        elif isinstance(token, bytes):
            token_bytes = token
        else:
            self._reject()
        if len(token_bytes) > self.max_token_bytes:
            self._reject()

        parts = token_bytes.decode("ascii").split(".")
        if len(parts) != 5:
            self._reject()
        version, purpose, payload_b64, footer_b64, signature_b64 = parts
        if version != self.version or purpose != self.purpose:
            self._reject()

        try:
            footer = json.loads(self._b64decode(footer_b64))
        except (ValueError, binascii.Error):
            self._reject()
        if not isinstance(footer, dict):
            self._reject()
        kid = footer.get("kid")
        entry = self.trusted_keys.get(kid)
        if not isinstance(entry, dict):
            self._reject()
        if entry.get("version") != self.version or entry.get("purpose") != self.purpose:
            self._reject()
        if not (entry.get("active") or entry.get("grace")):
            self._reject()

        try:
            signature = self._b64decode(signature_b64)
        except (ValueError, binascii.Error):
            self._reject()
        signing = "%s.%s.%s.%s.%s" % (version, purpose, payload_b64,
                                      footer_b64, implicit)
        expected = hmac.new(entry["key"], signing.encode("utf-8"),
                            hashlib.sha256).digest()
        if not hmac.compare_digest(expected, signature):
            self._reject()

        try:
            payload = json.loads(self._b64decode(payload_b64))
        except (ValueError, binascii.Error):
            self._reject()
        if not isinstance(payload, dict):
            self._reject()

        skew = self.tenant_skew.get(tenant, 0)
        now_value = self._numeric(now)
        exp = self._numeric(payload.get("exp"))
        if exp is None or now_value > exp + skew:
            self._reject()
        nbf = self._numeric(payload.get("nbf"))
        if nbf is not None and now_value + skew < nbf:
            self._reject()

        if audience is not None:
            aud = payload.get("aud")
            if isinstance(aud, list):
                if audience not in aud:
                    self._reject()
            elif aud != audience:
                self._reject()

        nonce = payload.get("nonce")
        if not nonce:
            self._reject()
        nonce_key = (tenant, nonce)
        with self._nonce_lock:
            if nonce_key in self._used_nonces:
                self._reject()
            self._used_nonces.add(nonce_key)

        return payload

    def _numeric(self, value):
        if isinstance(value, bool):
            self._reject()
        if isinstance(value, (int, float)):
            return value
        if isinstance(value, str):
            try:
                return float(value)
            except ValueError:
                self._reject()
        return None
