"""OpenID4VP presentation mapping and validation skeleton."""

import threading
import uuid

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
        self._sessions = {}
        self._lock = threading.Lock()

    def create_request(self, session_id, input_descriptors, nonce=None):
        nonce = nonce or uuid.uuid4().hex
        request = {
            "session_id": session_id,
            "nonce": nonce,
            "input_descriptors": list(input_descriptors),
        }
        with self._lock:
            self._sessions[session_id] = {
                "nonce": nonce,
                "consumed": False,
            }
        return request

    def submit(self, session_id, request, presentation, now=0):
        session = self._consume_nonce(session_id, request)
        try:
            self._validate(request, presentation, now)
        except VpError:
            raise VpError()
        descriptor_map = presentation["descriptor_map"]
        subject = presentation["credentials"][
            descriptor_map[0]["credential"]]["claims"].get("sub")
        if subject is None:
            subject = descriptor_map[0]["credential"]
        return {"subject": subject, "session_id": session["session_id"]}

    def decode_direct_post(self, token, key):
        if not isinstance(token, dict) or not token.get("encrypted") or not key:
            raise VpError()
        return token.get("payload", token)

    def _consume_nonce(self, session_id, request):
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None or session["consumed"]:
                raise VpError()
            if request.get("nonce") != session["nonce"]:
                raise VpError()
            if request.get("session_id") != session_id:
                raise VpError()
            session["consumed"] = True
            session["session_id"] = session_id
            return session

    def _validate(self, request, presentation, now):
        if not presentation.get("encrypted"):
            raise VpError()

        descriptors = request.get("input_descriptors") or []
        descriptor_map = presentation.get("descriptor_map") or []

        expected_ids = [d["id"] for d in descriptors]
        submitted_ids = [d["id"] for d in descriptor_map]
        if len(submitted_ids) != len(set(submitted_ids)):
            raise VpError()
        if set(submitted_ids) != set(expected_ids):
            raise VpError()

        credential_refs = [d["credential"] for d in descriptor_map]
        if len(credential_refs) != len(set(credential_refs)):
            raise VpError()

        if self.require_kb:
            kb_jwt = presentation.get("kb_jwt") or {}
            if not kb_jwt.get("valid"):
                raise VpError()
            if kb_jwt.get("nonce") != request.get("nonce"):
                raise VpError()
            if kb_jwt.get("aud") != self.audience:
                raise VpError()

        formats = {d["id"]: d.get("format") for d in descriptors}
        credentials = presentation.get("credentials") or {}
        for entry in descriptor_map:
            expected_format = formats[entry["id"]]
            if expected_format and presentation.get("format") != expected_format:
                raise VpError()
            credential = credentials.get(entry["credential"])
            if credential is None:
                raise VpError()
            if expected_format and credential.get("format") != expected_format:
                raise VpError()
            self._validate_credential(entry["credential"], credential, now)

    def _validate_credential(self, credential_id, credential, now):
        valid_from = credential.get("valid_from")
        valid_until = credential.get("valid_until")
        if valid_from is not None and now < valid_from:
            raise VpError()
        if valid_until is not None and now >= valid_until:
            raise VpError()
        if not credential.get("disclosure_digests_valid", False):
            raise VpError()
        if not self.revocation_available:
            raise VpError()
        if credential.get("revoked") or credential_id in self.revoked:
            raise VpError()
