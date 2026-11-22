"""SASL SCRAM nonce, channel binding and proof state machine.

The nonce, channel-binding value and proof verification for a single
authentication exchange live in one per-connection state object.  All shared
server state (issued/consumed nonces, verifiers, rate-limit counters, audit
log) is guarded by a single lock so that concurrent connections cannot share
a challenge or race the replay/rate-limit checks.
"""

import base64
import binascii
import hashlib
import hmac
import secrets
import threading


# RFC 5802 nonce charset: printable ASCII excluding the comma.  The exclamation
# mark is additionally rejected to keep nonces to a strict alphanumerics plus
# safe-symbol alphabet.
_NONCE_ALLOWED = set(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    "+/=-_"
)
MIN_CLIENT_NONCE_LEN = 16
SERVER_NONCE_LEN = 24


class ScramError(Exception):
    def __init__(self, code, message=""):
        self.code = code
        super().__init__(code if not message else "%s: %s" % (code, message))


def _parse_attrs(message, expected):
    """Parse a SCRAM attribute list, enforcing order and required fields."""
    attrs = {}
    for part in message.split(","):
        if len(part) < 2 or part[1] != "=" or not part[0].isalpha():
            raise ScramError("BAD_REQUEST")
        key, value = part[0], part[2:]
        if key in attrs:
            raise ScramError("BAD_REQUEST")
        attrs[key] = value
    if not expected.issubset(attrs):
        raise ScramError("BAD_REQUEST")
    return attrs


def _b64decode(value):
    try:
        return base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError):
        raise ScramError("BAD_REQUEST")


class ScramChannelBinding:
    def __init__(self, min_iterations=4096, max_failures=5):
        self.min_iterations = min_iterations
        self.max_failures = max_failures
        self._lock = threading.RLock()
        self._users = {}
        self._sessions = {}
        self._issued_nonces = set()
        self._consumed_nonces = set()
        self._failures = {}
        self._audit = []

    # ------------------------------------------------------------------ users
    def _validate_verifier(self, salt, iterations):
        if not salt:
            raise ScramError("EMPTY_SALT")
        if not isinstance(iterations, int) or iterations < self.min_iterations:
            raise ScramError("ITERATIONS_TOO_LOW")

    def register_user(self, username, *, salt, stored_key, server_key,
                      iterations=4096):
        self._validate_verifier(salt, iterations)
        with self._lock:
            self._users[username] = {
                "salt": bytes(salt),
                "stored_key": bytes(stored_key),
                "server_key": bytes(server_key),
                "iterations": iterations,
            }

    def change_password(self, username, *, salt, stored_key, server_key,
                        iterations=4096):
        self._validate_verifier(salt, iterations)
        with self._lock:
            if username not in self._users:
                raise ScramError("AUTH_FAILED")
            self._users[username] = {
                "salt": bytes(salt),
                "stored_key": bytes(stored_key),
                "server_key": bytes(server_key),
                "iterations": iterations,
            }

    # ----------------------------------------------------------------- start
    def start(self, client_first, *, connection_id="", tls_binding=b"",
              client_ip="0.0.0.0"):
        # An optional GS2 header ("n,," / "y,," / "p=..,,") may prefix the
        # bare client-first message; strip it when present.
        bare = client_first
        if client_first[:2] in ("n,", "y,") or client_first.startswith("p="):
            header_end = client_first.find(",,")
            if header_end == -1:
                raise ScramError("BAD_REQUEST")
            bare = client_first[header_end + 2:]
        if "," not in bare:
            raise ScramError("BAD_REQUEST")
        attrs = _parse_attrs(bare, {"n", "r"})
        username = attrs["n"]
        client_nonce = attrs["r"]

        if (len(client_nonce) < MIN_CLIENT_NONCE_LEN
                or any(ch not in _NONCE_ALLOWED for ch in client_nonce)):
            raise ScramError("BAD_NONCE")

        # The server picks the iteration count; anything the client claims is
        # ignored but a count below the floor is rejected outright.
        if "i" in attrs:
            try:
                claimed = int(attrs["i"])
            except ValueError:
                raise ScramError("BAD_REQUEST")
            if claimed < self.min_iterations:
                raise ScramError("ITERATIONS_TOO_LOW")

        with self._lock:
            rate_key = (username, client_ip)
            full_nonce = client_nonce + self._mint_server_nonce()
            while (full_nonce in self._issued_nonces
                   or full_nonce in self._consumed_nonces):
                full_nonce = client_nonce + self._mint_server_nonce()

            record = self._users.get(username)
            if record is None:
                # Identical response to a bad password: no state, no hint.
                self._register_failure(rate_key)
                self._audit.append({
                    "event": "auth_failed",
                    "user": username,
                    "client_ip": client_ip,
                    "nonce": full_nonce,
                    "iterations": None,
                    "binding": "n/a",
                    "reason": "AUTH_FAILED",
                })
                raise ScramError("AUTH_FAILED")

            server_first = "r=%s,s=%s,i=%d" % (
                full_nonce,
                base64.b64encode(record["salt"]).decode(),
                record["iterations"],
            )
            self._issued_nonces.add(full_nonce)
            self._sessions[full_nonce] = {
                "connection_id": connection_id,
                "client_ip": client_ip,
                "username": username,
                "client_first_bare": bare,
                "server_first": server_first,
                "tls_binding": bytes(tls_binding),
                "iterations": record["iterations"],
            }
            return {"nonce": full_nonce, "server_first": server_first}

    # ---------------------------------------------------------------- finish
    def finish(self, client_final, *, client_ip="0.0.0.0"):
        if "," not in client_final:
            raise ScramError("BAD_REQUEST")
        attrs = _parse_attrs(client_final, {"c", "r", "p"})
        full_nonce = attrs["r"]
        channel_binding = _b64decode(attrs["c"])
        proof = _b64decode(attrs["p"])

        with self._lock:
            # Replay check wins over every other interpretation: a nonce that
            # already completed an exchange can never be used again.
            if full_nonce in self._consumed_nonces:
                raise ScramError("REPLAY")

            session = self._sessions.get(full_nonce)
            if session is None:
                raise ScramError("BAD_REQUEST")

            # The rate-limit identity is bound to the connection that opened
            # the exchange, not to whoever replays the final message.
            rate_key = (session["username"], session["client_ip"])
            if self._failures.get(rate_key, 0) >= self.max_failures:
                raise ScramError("ERATELIMIT")

            # Hard gate: byte-for-byte equality with the TLS export value.
            if not hmac.compare_digest(channel_binding,
                                       session["tls_binding"]):
                self._fail(
                    session, full_nonce, session["client_ip"],
                    binding="mismatch", code="CHANNEL_BINDING_MISMATCH",
                )

            # Re-read the verifier at finish time so a password change
            # invalidates every in-flight exchange using the old verifier.
            record = self._users.get(session["username"])
            if record is None or len(proof) != hashlib.sha256().digest_size:
                self._fail(session, full_nonce, session["client_ip"],
                           binding="ok", code="AUTH_FAILED")

            final_without_proof = client_final.rsplit(",", 1)[0]
            auth_message = self.auth_message(
                session["client_first_bare"],
                session["server_first"],
                final_without_proof,
            )
            client_signature = hmac.new(
                record["stored_key"], auth_message.encode(), hashlib.sha256
            ).digest()
            expected_client_key = bytes(
                a ^ b for a, b in zip(proof, client_signature)
            )
            expected_stored_key = hashlib.sha256(
                expected_client_key
            ).digest()

            # Constant-time comparison so proof bytes cannot be probed via
            # timing differences.
            if not hmac.compare_digest(expected_stored_key,
                                       record["stored_key"]):
                self._fail(session, full_nonce, session["client_ip"],
                           binding="ok", code="AUTH_FAILED")

            server_signature = base64.b64encode(
                hmac.new(record["server_key"], auth_message.encode(),
                         hashlib.sha256).digest()
            ).decode()

            self._consumed_nonces.add(full_nonce)
            self._issued_nonces.discard(full_nonce)
            del self._sessions[full_nonce]
            self._failures.pop(rate_key, None)
            self._audit.append({
                "event": "auth_success",
                "user": session["username"],
                "client_ip": session["client_ip"],
                "nonce": full_nonce,
                "iterations": session["iterations"],
                "binding": "ok",
            })
            return {
                "authenticated": True,
                "user": session["username"],
                "server_signature": server_signature,
            }

    def _fail(self, session, full_nonce, client_ip, *, binding, code):
        rate_key = (session["username"], client_ip)
        self._register_failure(rate_key)
        self._audit.append({
            "event": "auth_failed",
            "user": session["username"],
            "client_ip": client_ip,
            "nonce": full_nonce,
            "iterations": session["iterations"],
            "binding": binding,
            "reason": code,
        })
        raise ScramError(code)

    def _register_failure(self, rate_key):
        self._failures[rate_key] = self._failures.get(rate_key, 0) + 1

    def _mint_server_nonce(self):
        return secrets.token_urlsafe(SERVER_NONCE_LEN)

    # ------------------------------------------------------------- utilities
    def auth_message(self, client_first_bare, server_first,
                     client_final_without_proof):
        # RFC 5802 section 3: client-first-bare, server-first,
        # client-final-without-proof, concatenated in that exact order.
        return "%s,%s,%s" % (
            client_first_bare, server_first, client_final_without_proof
        )

    def audit_events(self):
        with self._lock:
            return list(self._audit)
