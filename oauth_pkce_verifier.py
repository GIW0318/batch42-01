"""OAuth authorization-code plus PKCE one-time grant gate."""

import base64
import hashlib
import hmac
import re
import secrets
import threading


_VERIFIER_RE = re.compile(r"^[A-Za-z0-9\-._~]{43,128}$")
_S256_CHALLENGE_RE = re.compile(r"^[A-Za-z0-9\-_]{43}$")


class OAuthError(Exception):
    def __init__(self, code, message=""):
        self.code = code
        self.status_code = 400
        self.http_status = 400
        super().__init__(code if not message else "%s: %s" % (code, message))


def _b64url(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _normalize_scope(scope):
    if scope is None:
        return ""
    if not isinstance(scope, str):
        raise OAuthError("invalid_scope")
    return " ".join(scope.split())


def _scope_set(scope):
    return set(scope.split())


class GrantExchangeGate:
    def __init__(self, code_ttl=600):
        self.code_ttl = code_ttl
        self._lock = threading.RLock()
        self._clients = {}
        self._codes = {}
        self._chains = {}
        self._refresh_tokens = {}
        self._access_tokens = {}
        self._audit = []

    def register_client(self, client_id, *, client_type="public",
                        redirect_uris=()):
        if not isinstance(client_id, str) or not client_id:
            raise OAuthError("invalid_client")
        if client_type not in ("public", "confidential"):
            raise OAuthError("invalid_client")
        uris = []
        for uri in redirect_uris:
            if isinstance(uri, str) and uri not in uris:
                uris.append(uri)
        with self._lock:
            self._clients[client_id] = {
                "client_type": client_type,
                "redirect_uris": uris,
            }

    def authorize(self, client_id, redirect_uri, scope, *, code_challenge=None,
                  method="S256", now=0.0):
        with self._lock:
            client = self._clients.get(client_id)
            if client is None:
                self._audit_event("authorize", "invalid_client", client_id)
                raise OAuthError("invalid_client")

            is_public = client["client_type"] == "public"
            if is_public and code_challenge is None:
                self._audit_event("authorize", "pkce_required", client_id)
                raise OAuthError("pkce_required")

            if code_challenge is None:
                if method not in (None, "S256"):
                    self._audit_event("authorize", "invalid_request", client_id)
                    raise OAuthError("invalid_request")
            else:
                if method != "S256":
                    self._audit_event("authorize", "invalid_request", client_id)
                    raise OAuthError("invalid_request")
                if (not isinstance(code_challenge, str)
                        or _S256_CHALLENGE_RE.fullmatch(code_challenge) is None):
                    self._audit_event("authorize", "invalid_request", client_id)
                    raise OAuthError("invalid_request")

            if redirect_uri not in client["redirect_uris"]:
                self._audit_event("authorize", "invalid_redirect", client_id)
                raise OAuthError("invalid_redirect")

            normalized_scope = _normalize_scope(scope)
            code = secrets.token_urlsafe(32)
            self._codes[code] = {
                "client_id": client_id,
                "redirect_uri": redirect_uri,
                "scope": normalized_scope,
                "code_challenge": code_challenge,
                "method": method,
                "issued_at": now,
                "expires_at": now + self.code_ttl,
                "status": "active",
            }
            self._audit_event("authorize", "ok", client_id)
            return {"code": code}

    def exchange(self, code, client_id, redirect_uri, *, code_verifier=None,
                 now=0.0):
        with self._lock:
            record = self._codes.get(code)
            if record is None:
                self._audit_event("exchange", "invalid_grant", client_id)
                raise OAuthError("invalid_grant")

            if record["status"] in ("used", "burned"):
                self._audit_event("exchange", "code_reused", client_id)
                raise OAuthError("code_reused")
            if record["status"] == "expired" or now >= record["expires_at"]:
                record["status"] = "expired"
                self._audit_event("exchange", "expired", client_id)
                raise OAuthError("expired")

            if (record["client_id"] != client_id
                    or record["redirect_uri"] != redirect_uri):
                self._audit_event("exchange", "invalid_grant", client_id)
                raise OAuthError("invalid_grant")

            challenge = record["code_challenge"]
            client = self._clients.get(client_id)
            is_public = client is not None and client["client_type"] == "public"

            if challenge is None:
                if is_public:
                    self._audit_event("exchange", "pkce_required", client_id)
                    raise OAuthError("pkce_required")
            else:
                if code_verifier is None:
                    if is_public:
                        self._audit_event("exchange", "pkce_required", client_id)
                        raise OAuthError("pkce_required")
                    self._burn_code(record)
                    self._audit_event("exchange", "bad_verifier", client_id)
                    raise OAuthError("bad_verifier")
                if (not isinstance(code_verifier, str)
                        or _VERIFIER_RE.fullmatch(code_verifier) is None):
                    self._burn_code(record)
                    self._audit_event("exchange", "bad_verifier", client_id)
                    raise OAuthError("bad_verifier")
                actual_challenge = _b64url(
                    hashlib.sha256(code_verifier.encode("ascii")).digest()
                )
                if not hmac.compare_digest(actual_challenge, challenge):
                    self._burn_code(record)
                    self._audit_event("exchange", "bad_verifier", client_id)
                    raise OAuthError("bad_verifier")

            record["status"] = "used"
            scope = record["scope"]
            access_token = secrets.token_urlsafe(32)
            refresh_token = secrets.token_urlsafe(32)
            chain_id = secrets.token_urlsafe(24)
            self._chains[chain_id] = {
                "client_id": client_id,
                "redirect_uri": redirect_uri,
                "scope": scope,
                "status": "active",
                "current_refresh_token": refresh_token,
                "refresh_tokens": {refresh_token},
                "access_tokens": {access_token},
            }
            self._refresh_tokens[refresh_token] = {
                "chain_id": chain_id,
                "client_id": client_id,
                "scope": scope,
                "status": "active",
                "issued_at": now,
            }
            self._access_tokens[access_token] = {
                "chain_id": chain_id,
                "client_id": client_id,
                "status": "active",
            }
            self._audit_event("exchange", "ok", client_id)
            return {
                "access_token": access_token,
                "refresh_token": refresh_token,
                "token_type": "Bearer",
                "scope": scope,
            }

    def refresh(self, refresh_token, client_id, *, scope=None, now=0.0):
        with self._lock:
            record = self._refresh_tokens.get(refresh_token)
            if record is None or record["client_id"] != client_id:
                self._audit_event("refresh", "invalid_grant", client_id)
                raise OAuthError("invalid_grant")

            chain = self._chains[record["chain_id"]]
            if record["status"] == "rotated":
                self._revoke_chain(chain)
                self._audit_event("refresh", "token_reuse", client_id)
                raise OAuthError("token_reuse")
            if record["status"] == "revoked" or chain["status"] == "revoked":
                self._audit_event("refresh", "revoked", client_id)
                raise OAuthError("revoked")

            effective_scope = record["scope"]
            if scope is None:
                next_scope = effective_scope
            else:
                requested_scope = _normalize_scope(scope)
                if not _scope_set(requested_scope) <= _scope_set(effective_scope):
                    self._audit_event("refresh", "invalid_scope", client_id)
                    raise OAuthError("invalid_scope")
                next_scope = requested_scope

            new_access_token = secrets.token_urlsafe(32)
            new_refresh_token = secrets.token_urlsafe(32)
            record["status"] = "rotated"
            self._refresh_tokens[new_refresh_token] = {
                "chain_id": record["chain_id"],
                "client_id": client_id,
                "scope": next_scope,
                "status": "active",
                "issued_at": now,
            }
            self._access_tokens[new_access_token] = {
                "chain_id": record["chain_id"],
                "client_id": client_id,
                "status": "active",
            }
            chain["current_refresh_token"] = new_refresh_token
            chain["refresh_tokens"].add(new_refresh_token)
            chain["access_tokens"].add(new_access_token)
            chain["scope"] = next_scope
            self._audit_event("refresh", "ok", client_id)
            return {
                "access_token": new_access_token,
                "refresh_token": new_refresh_token,
                "token_type": "Bearer",
                "scope": next_scope,
            }

    def snapshot(self):
        with self._lock:
            chains = {}
            for chain_id, chain in self._chains.items():
                chains[chain_id] = {
                    "client_id": chain["client_id"],
                    "redirect_uri": chain["redirect_uri"],
                    "scope": chain["scope"],
                    "status": chain["status"],
                    "current_refresh_token": chain["current_refresh_token"],
                    "refresh_tokens": sorted(chain["refresh_tokens"]),
                    "access_tokens": sorted(chain["access_tokens"]),
                }
            return {
                "version": 1,
                "code_ttl": self.code_ttl,
                "clients": {
                    client_id: {
                        "client_type": client["client_type"],
                        "redirect_uris": list(client["redirect_uris"]),
                    }
                    for client_id, client in self._clients.items()
                },
                "codes": {
                    code: dict(record)
                    for code, record in self._codes.items()
                },
                "chains": chains,
                "refresh_tokens": {
                    token: dict(record)
                    for token, record in self._refresh_tokens.items()
                },
                "access_tokens": {
                    token: dict(record)
                    for token, record in self._access_tokens.items()
                },
                "audit": [dict(event) for event in self._audit],
            }

    def restore(self, snapshot):
        if not isinstance(snapshot, dict):
            raise OAuthError("invalid_request")
        with self._lock:
            self.code_ttl = snapshot.get("code_ttl", self.code_ttl)
            self._clients = {
                client_id: {
                    "client_type": client["client_type"],
                    "redirect_uris": list(client.get("redirect_uris", ())),
                }
                for client_id, client in snapshot.get("clients", {}).items()
            }
            self._codes = {
                code: dict(record)
                for code, record in snapshot.get("codes", {}).items()
            }
            self._chains = {
                chain_id: {
                    "client_id": chain["client_id"],
                    "redirect_uri": chain["redirect_uri"],
                    "scope": chain["scope"],
                    "status": chain["status"],
                    "current_refresh_token": chain["current_refresh_token"],
                    "refresh_tokens": set(chain.get("refresh_tokens", ())),
                    "access_tokens": set(chain.get("access_tokens", ())),
                }
                for chain_id, chain in snapshot.get("chains", {}).items()
            }
            self._refresh_tokens = {
                token: dict(record)
                for token, record in snapshot.get("refresh_tokens", {}).items()
            }
            self._access_tokens = {
                token: dict(record)
                for token, record in snapshot.get("access_tokens", {}).items()
            }
            self._audit = [dict(event) for event in snapshot.get("audit", ())]

    def audit_events(self):
        with self._lock:
            return [dict(event) for event in self._audit]

    def _audit_event(self, event, outcome, client_id=None):
        entry = {"event": event, "outcome": outcome}
        if client_id is not None:
            entry["client_id"] = client_id
        self._audit.append(entry)

    @staticmethod
    def _burn_code(record):
        record["status"] = "burned"

    def _revoke_chain(self, chain):
        chain["status"] = "revoked"
        for token in chain["refresh_tokens"]:
            record = self._refresh_tokens.get(token)
            if record is not None:
                record["status"] = "revoked"
        for token in chain["access_tokens"]:
            record = self._access_tokens.get(token)
            if record is not None:
                record["status"] = "revoked"
