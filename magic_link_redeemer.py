"""One-time magic link issuance and redemption."""

import copy
import hashlib
import hmac
import secrets
import threading


INVALID_TOKEN = "invalid-token"
INVALID_REDIRECT = "invalid-redirect"
RATE_LIMITED = "rate-limited"


class MagicLinkError(Exception):
    def __init__(self, code, message=""):
        self.code = code
        super().__init__(code if not message else "%s: %s" % (code, message))


class MagicLinkRedeemer:
    """Salted token digests, one-time redemption and email-bound links.

    Tokens are returned in clear only from issue()/resend(); snapshots keep
    digests only.  request_id makes a repeated redemption idempotent while a
    different request id is rejected.  Rate limiting is per normalized email,
    independent of the source address.
    """

    def __init__(self, secret, ttl=900, redirect_allowlist=(),
                 max_requests_per_hour=5):
        self.secret = secret
        if isinstance(secret, str):
            secret = secret.encode("utf-8")
        self._secret = bytes(secret)
        self.ttl = ttl
        self.redirect_allowlist = tuple(redirect_allowlist)
        self.max_requests_per_hour = max_requests_per_hour
        self._lock = threading.RLock()
        self._tokens = {}
        self._current_token = {}
        self._rate_events = {}

    def normalize_email(self, email):
        if not isinstance(email, str):
            raise ValueError("email must be a string")
        normalized = email.strip().lower()
        if normalized.count("@") != 1:
            raise ValueError("invalid email address")
        local_part, domain = normalized.split("@", 1)
        local_part = local_part.split("+", 1)[0]
        if not local_part or not domain:
            raise ValueError("invalid email address")
        return "%s@%s" % (local_part, domain)

    def issue(self, email, redirect_url=None, now=0, source_ip=""):
        return self._issue(email, redirect_url, now, source_ip)

    def resend(self, email, redirect_url=None, now=0, source_ip=""):
        return self._issue(email, redirect_url, now, source_ip)

    def preview(self, token, now=0):
        digest = self._digest(token)
        with self._lock:
            record = self._tokens.get(digest)
            if not self._is_usable(record, now):
                raise MagicLinkError(INVALID_TOKEN)
            return {
                "email": record["email"],
                "redirect": record["redirect"],
            }

    def redeem(self, token, email, redirect_url=None, now=0, request_id=None):
        try:
            normalized_email = self.normalize_email(email)
        except (TypeError, ValueError):
            raise MagicLinkError(INVALID_TOKEN) from None
        digest = self._digest(token)
        with self._lock:
            record = self._tokens.get(digest)
            if record is None or record["email"] != normalized_email:
                raise MagicLinkError(INVALID_TOKEN)

            if record["used"]:
                if (request_id is not None
                        and record["request_id"] == request_id):
                    return {
                        "email": record["email"],
                        "redirect": record["redirect"],
                        "session": record["session"],
                        "duplicate": True,
                    }
                raise MagicLinkError(INVALID_TOKEN)

            if not self._is_usable(record, now):
                raise MagicLinkError(INVALID_TOKEN)

            final_redirect = record["redirect"]
            if redirect_url is not None:
                if redirect_url not in self.redirect_allowlist:
                    raise MagicLinkError(INVALID_REDIRECT)
                if final_redirect is not None and redirect_url != final_redirect:
                    raise MagicLinkError(INVALID_REDIRECT)
                final_redirect = redirect_url

            session = secrets.token_urlsafe(32)
            record["used"] = True
            record["used_at"] = now
            record["request_id"] = request_id
            record["session"] = session
            record["redirect"] = final_redirect
            self._current_token.pop(normalized_email, None)

            return {
                "email": record["email"],
                "redirect": final_redirect,
                "session": session,
                "duplicate": False,
            }

    def request_count(self, email):
        normalized_email = self.normalize_email(email)
        with self._lock:
            return len(self._rate_events.get(normalized_email, ()))

    def snapshot(self):
        with self._lock:
            return copy.deepcopy({
                "version": 1,
                "tokens": self._tokens,
                "current_token": self._current_token,
                "rate_events": self._rate_events,
            })

    def restore(self, snapshot):
        if not isinstance(snapshot, dict):
            raise ValueError("invalid snapshot")
        tokens = snapshot.get("tokens", {})
        current_token = snapshot.get("current_token", {})
        rate_events = snapshot.get("rate_events", {})
        if not isinstance(tokens, dict) or not isinstance(current_token, dict):
            raise ValueError("invalid snapshot")
        if not isinstance(rate_events, dict):
            raise ValueError("invalid snapshot")

        with self._lock:
            self._tokens = copy.deepcopy(tokens)
            self._current_token = copy.deepcopy(current_token)
            self._rate_events = {
                email: list(events)
                for email, events in copy.deepcopy(rate_events).items()
            }

    def _issue(self, email, redirect_url, now, source_ip):
        normalized_email = self.normalize_email(email)
        if (redirect_url is not None
                and redirect_url not in self.redirect_allowlist):
            raise MagicLinkError(INVALID_REDIRECT)

        with self._lock:
            self._check_rate_limit(normalized_email, now)

            previous_digest = self._current_token.get(normalized_email)
            if previous_digest is not None:
                previous = self._tokens.get(previous_digest)
                if previous is not None:
                    previous["revoked"] = True

            token = secrets.token_urlsafe(32)
            digest = self._digest(token)
            while digest in self._tokens:
                token = secrets.token_urlsafe(32)
                digest = self._digest(token)

            self._tokens[digest] = {
                "email": normalized_email,
                "redirect": redirect_url,
                "issued_at": now,
                "expires_at": now + self.ttl,
                "revoked": False,
                "used": False,
                "used_at": None,
                "request_id": None,
                "session": None,
            }
            self._current_token[normalized_email] = digest
            self._rate_events.setdefault(normalized_email, []).append(now)
            return token

    def _check_rate_limit(self, normalized_email, now):
        cutoff = now - 3600
        events = [
            timestamp
            for timestamp in self._rate_events.get(normalized_email, ())
            if timestamp > cutoff
        ]
        self._rate_events[normalized_email] = events
        if len(events) >= self.max_requests_per_hour:
            raise MagicLinkError(RATE_LIMITED)

    def _digest(self, token):
        if not isinstance(token, str):
            raise MagicLinkError(INVALID_TOKEN)
        return hmac.new(
            self._secret,
            token.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

    @staticmethod
    def _is_usable(record, now):
        return (
            record is not None
            and not record["revoked"]
            and not record["used"]
            and now < record["expires_at"]
        )
