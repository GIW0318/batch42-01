"""Password-reset token lifecycle and session revocation gate."""

import hashlib
import hmac
import secrets


class ResetTokenError(Exception):
    def __init__(self, code, message=""):
        self.code = code
        super().__init__(code if not message else "%s: %s" % (code, message))


def _normalize_email(email):
    local, _, domain = (email or "").strip().lower().partition("@")
    local = local.split("+", 1)[0]
    return local + ("@" + domain if domain else "")


def _hash_token(token):
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class ResetTokenGate:
    def __init__(self, token_ttl=1800, max_requests=3, request_window=3600):
        self.token_ttl = token_ttl
        self.max_requests = max_requests
        self.request_window = request_window
        self._accounts = {}
        self._sessions = {}
        self._remember = {}
        self._tokens = {}
        self._requests = {}
        self._audit = []
        self._seq = 0

    def add_account(self, account, email):
        self._accounts[account] = {
            "email": _normalize_email(email),
            "deleted": False,
        }
        self._sessions.setdefault(account, [])
        self._remember.setdefault(account, [])
        self._requests.setdefault(account, [])

    def delete_account(self, account):
        if account not in self._accounts:
            return
        self._accounts[account]["deleted"] = True
        self._sessions[account] = []
        self._remember[account] = []
        for record in self._tokens.values():
            if record["account"] == account and record["status"] == "pending":
                record["status"] = "invalidated"

    def add_session(self, account):
        self._seq += 1
        session_id = "sess-%d" % self._seq
        self._sessions.setdefault(account, []).append(session_id)
        return session_id

    def add_remember_token(self, account):
        self._seq += 1
        remember_id = "rem-%d" % self._seq
        self._remember.setdefault(account, []).append(remember_id)
        return remember_id

    def active_sessions(self, account):
        return len(self._sessions.get(account, []))

    def active_remember_tokens(self, account):
        return len(self._remember.get(account, []))

    def pending_tokens(self, account):
        return sum(
            1
            for record in self._tokens.values()
            if record["account"] == account and record["status"] == "pending"
        )

    def request_reset(self, account, *, email=None, ip="0.0.0.0", now=0.0,
                      mailer=None):
        acct = self._accounts.get(account)
        if acct is None or acct["deleted"]:
            _hash_token(secrets.token_urlsafe(32))
            return {"status": "accepted"}
        if email is not None and _normalize_email(email) != acct["email"]:
            _hash_token(secrets.token_urlsafe(32))
            return {"status": "accepted"}
        history = self._requests.setdefault(account, [])
        cutoff = now - self.request_window
        history[:] = [stamp for stamp in history if stamp >= cutoff]
        if len(history) >= self.max_requests:
            raise ResetTokenError("rate_limited", "too many reset requests")
        history.append(now)
        token = secrets.token_urlsafe(32)
        try:
            if mailer is not None:
                mailer(acct["email"], token)
        except Exception:
            return {"status": "mail_failed"}
        for record in self._tokens.values():
            if record["account"] == account and record["status"] == "pending":
                record["status"] = "superseded"
        self._tokens[_hash_token(token)] = {
            "account": account,
            "issued_at": now,
            "expires_at": now + self.token_ttl,
            "status": "pending",
        }
        return {"status": "accepted"}

    def _find_token(self, token):
        digest = _hash_token(token or "")
        for candidate, record in self._tokens.items():
            if hmac.compare_digest(candidate, digest):
                return record
        return None

    def _revoke_account(self, account):
        revoked_sessions = len(self._sessions.get(account, []))
        revoked_remember = len(self._remember.get(account, []))
        self._sessions[account] = []
        self._remember[account] = []
        return revoked_sessions, revoked_remember

    def reset(self, token, new_password, *, now=0.0, ip=""):
        record = self._find_token(token)
        if record is None:
            raise ResetTokenError("invalid", "unknown token")
        if record["status"] == "used":
            raise ResetTokenError("reused", "token already used")
        if record["status"] == "superseded":
            raise ResetTokenError("superseded", "a newer link was issued")
        if record["status"] != "pending":
            raise ResetTokenError("invalid", "token is not usable")
        account = record["account"]
        acct = self._accounts.get(account)
        if acct is None or acct["deleted"]:
            raise ResetTokenError("invalid", "account unavailable")
        if now > record["expires_at"]:
            raise ResetTokenError("expired", "token expired")
        record["status"] = "used"
        for other in self._tokens.values():
            if (other is not record and other["account"] == account
                    and other["status"] == "pending"):
                other["status"] = "superseded"
        revoked_sessions, revoked_remember = self._revoke_account(account)
        self._audit.append({
            "action": "reset",
            "actor": account,
            "ip": ip,
            "time": now,
            "status": "ok",
            "revoked_sessions": revoked_sessions,
            "revoked_remember": revoked_remember,
        })
        return {
            "status": "ok",
            "revoked_sessions": revoked_sessions,
            "revoked_remember": revoked_remember,
        }

    def admin_reset(self, actor, account, *, confirm=False, now=0.0, ip=""):
        if not confirm:
            self._audit.append({
                "action": "admin_reset",
                "actor": actor,
                "account": account,
                "ip": ip,
                "time": now,
                "status": "confirmation_required",
            })
            raise ResetTokenError(
                "confirmation_required", "admin reset needs confirm=True"
            )
        acct = self._accounts.get(account)
        if acct is None or acct["deleted"]:
            raise ResetTokenError("invalid", "account unavailable")
        for record in self._tokens.values():
            if record["account"] == account and record["status"] == "pending":
                record["status"] = "superseded"
        revoked_sessions, revoked_remember = self._revoke_account(account)
        self._audit.append({
            "action": "admin_reset",
            "actor": actor,
            "account": account,
            "ip": ip,
            "time": now,
            "status": "ok",
            "revoked_sessions": revoked_sessions,
            "revoked_remember": revoked_remember,
        })
        return {
            "status": "ok",
            "revoked_sessions": revoked_sessions,
            "revoked_remember": revoked_remember,
        }

    def snapshot(self):
        return {
            "token_ttl": self.token_ttl,
            "max_requests": self.max_requests,
            "request_window": self.request_window,
            "accounts": {k: dict(v) for k, v in self._accounts.items()},
            "sessions": {k: list(v) for k, v in self._sessions.items()},
            "remember": {k: list(v) for k, v in self._remember.items()},
            "tokens": {h: dict(r) for h, r in self._tokens.items()},
            "requests": {k: list(v) for k, v in self._requests.items()},
            "audit": [dict(e) for e in self._audit],
            "seq": self._seq,
        }

    def restore(self, snapshot):
        self.token_ttl = snapshot.get("token_ttl", self.token_ttl)
        self.max_requests = snapshot.get("max_requests", self.max_requests)
        self.request_window = snapshot.get(
            "request_window", self.request_window
        )
        self._accounts = {k: dict(v) for k, v in snapshot["accounts"].items()}
        self._sessions = {k: list(v) for k, v in snapshot["sessions"].items()}
        self._remember = {k: list(v) for k, v in snapshot["remember"].items()}
        self._tokens = {h: dict(r) for h, r in snapshot["tokens"].items()}
        self._requests = {k: list(v) for k, v in snapshot["requests"].items()}
        self._audit = [dict(e) for e in snapshot["audit"]]
        self._seq = snapshot.get("seq", 0)

    def audit_events(self):
        return [dict(e) for e in self._audit]
