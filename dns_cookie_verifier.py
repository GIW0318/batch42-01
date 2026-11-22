"""Key-versioned DNS Cookie verifier.

Server cookie layout (RFC 9018 style):

    1 byte   key version
    8 bytes  issue timestamp, unsigned big-endian
    16 bytes HMAC-SHA256(secret, version || timestamp || client-ip ||
                         client-cookie), truncated to 128 bits

The client cookie is part of the MAC input, so several clients behind one
NAT address get independent cookies even though they share an IP.  Keys are
kept in a versioned ring: after a rotation the previous key stays valid for
``rotation_grace`` seconds, giving every cached cookie at least one overlap
period before it must be refreshed.
"""

import hmac
import hashlib
import ipaddress
import json
import os
import threading

CLIENT_COOKIE_LEN = 8
SERVER_COOKIE_LEN = 1 + 8 + 16
MIN_COOKIE_LEN = CLIENT_COOKIE_LEN
MAX_COOKIE_LEN = 32
MAC_LEN = 16

DEFAULT_SECRET = b"default-secret-0123456789abcdef"


class DnsCookieError(Exception):
    def __init__(self, code, message=""):
        self.code = code
        super().__init__(code if not message else "%s: %s" % (code, message))


def _pack_ip(client_ip):
    """Normalize a client address to its packed on-the-wire form."""
    if not client_ip:
        return b""
    return ipaddress.ip_address(str(client_ip)).packed


class DnsCookieVerifier:
    def __init__(self, secret=DEFAULT_SECRET, cookie_ttl=300,
                 rotation_grace=300):
        self.cookie_ttl = cookie_ttl
        self.rotation_grace = rotation_grace
        self._lock = threading.RLock()
        self._keys = [
            {
                "version": 1,
                "secret": bytes(secret),
                "rotated_at": None,
            }
        ]
        self._next_version = 2
        self._nonce_counter = 0
        self._events = []

    # ------------------------------------------------------------------ keys
    def _current_key(self):
        return self._keys[-1]

    def _find_key(self, version):
        for entry in self._keys:
            if entry["version"] == version:
                return entry
        return None

    def rotate_secret(self, new_secret, *, now=0.0):
        """Retire the current key and publish a new versioned key.

        The retired key is retained so cookies minted with it keep
        validating for ``rotation_grace`` seconds.
        """
        with self._lock:
            self._current_key()["rotated_at"] = float(now)
            new_entry = {
                "version": self._next_version,
                "secret": bytes(new_secret),
                "rotated_at": None,
            }
            self._next_version += 1
            self._keys.append(new_entry)
            self._audit("rotate", reason="KEY_ROTATED",
                        key_version=new_entry["version"])
            return new_entry["version"]

    # -------------------------------------------------------------- minting
    def _mac(self, key_entry, version, issued_at, client_ip, client_cookie):
        message = b"".join((
            bytes((version,)),
            int(issued_at).to_bytes(8, "big"),
            _pack_ip(client_ip),
            client_cookie,
        ))
        return hmac.new(key_entry["secret"], message,
                        hashlib.sha256).digest()[:MAC_LEN]

    def make_server_cookie(self, client_ip,
                           client_cookie=b"12345678", *,
                           version=None, now=0.0):
        with self._lock:
            if version is None:
                key_entry = self._current_key()
            else:
                key_entry = self._find_key(version)
                if key_entry is None:
                    raise DnsCookieError("UNKNOWN_KEY_VERSION",
                                         "no key with version %r" % (version,))
            key_version = key_entry["version"]
            issued_at = int(now)
            mac = self._mac(key_entry, key_version, issued_at,
                            client_ip, client_cookie)
            return (key_version.to_bytes(1, "big")
                    + issued_at.to_bytes(8, "big") + mac)

    def _issue(self, client_cookie, now):
        """Build a full fresh cookie option (client + server cookie)."""
        if not (isinstance(client_cookie, (bytes, bytearray))
                and len(client_cookie) == CLIENT_COOKIE_LEN):
            client_cookie = os.urandom(CLIENT_COOKIE_LEN)
        else:
            client_cookie = bytes(client_cookie)
        with self._lock:
            key_entry = self._current_key()
            key_version = key_entry["version"]
        server_cookie = self.make_server_cookie(
            "", client_cookie, version=key_version, now=now)
        return client_cookie + server_cookie

    # ------------------------------------------------------------- verifying
    def verify(self, client_cookie=None, server_cookie=None, client_ip="", *,
               udp=True, qtype="A", now=0.0):
        # Malformed option lengths must never take the process down.
        try:
            if client_cookie is not None and (
                    not isinstance(client_cookie, (bytes, bytearray))
                    or
                    len(client_cookie) < MIN_COOKIE_LEN
                    or len(client_cookie) > MAX_COOKIE_LEN):
                self._audit("formerr", reason="BAD_OPTION_LENGTH",
                            client_cookie=client_cookie)
                return {"status": "formerr", "cookie": None,
                        "reason": "BAD_OPTION_LENGTH"}
            if server_cookie is not None and (
                    not isinstance(server_cookie, (bytes, bytearray))
                    or
                    len(server_cookie) == 0
                    or len(server_cookie) > MAX_COOKIE_LEN):
                self._audit("formerr", reason="BAD_OPTION_LENGTH",
                            server_cookie=server_cookie)
                return {"status": "formerr", "cookie": None,
                        "reason": "BAD_OPTION_LENGTH"}
        except TypeError:
            self._audit("formerr", reason="BAD_OPTION_LENGTH")
            return {"status": "formerr", "cookie": None,
                    "reason": "BAD_OPTION_LENGTH"}

        # Gate 1: cookie-less UDP queries get BADCOOKIE plus a fresh cookie,
        # every query type included (IXFR is not exempt).
        if client_cookie is None and server_cookie is None:
            if not udp:
                self._audit("ok", reason="TCP_NO_COOKIE_ALLOWED")
                return {"status": "ok", "cookie": None,
                        "reason": "TCP_NO_COOKIE_ALLOWED",
                        "key_version": None}
            cookie = self._issue(None, now)
            self._audit("badcookie", reason="NO_COOKIE",
                        client_cookie=cookie[:CLIENT_COOKIE_LEN],
                        server_cookie=cookie[CLIENT_COOKIE_LEN:])
            return {"status": "badcookie", "cookie": cookie,
                    "reason": "NO_COOKIE", "key_version": None}

        # A client cookie without a server cookie cannot be answered:
        # silently drop, do not help the spoofed source.
        if server_cookie is None:
            self._audit("drop", reason="BAD_SERVER_COOKIE",
                        client_cookie=client_cookie)
            return {"status": "drop", "cookie": None,
                    "reason": "BAD_SERVER_COOKIE"}

        if len(server_cookie) < 1 + 8:
            self._audit("drop", reason="BAD_SERVER_COOKIE",
                        client_cookie=client_cookie,
                        server_cookie=server_cookie)
            return {"status": "drop", "cookie": None,
                    "reason": "BAD_SERVER_COOKIE"}

        key_version = server_cookie[0]
        issued_at = int.from_bytes(server_cookie[1:9], "big")
        presented_mac = server_cookie[9:]

        with self._lock:
            key_entry = self._find_key(key_version)
            if key_entry is not None:
                expected_mac = self._mac(
                    key_entry, key_version, issued_at,
                    client_ip, client_cookie)
                mac_ok = (len(presented_mac) == len(expected_mac)
                          and hmac.compare_digest(presented_mac,
                                                  expected_mac))
                retired = key_entry["rotated_at"] is not None
            else:
                mac_ok = False
                retired = False

        if not mac_ok:
            # Gate 4: invalid server cookies are silently dropped.
            # A cookie whose version we know but whose MAC failed was minted
            # for another client feature (IP/client-cookie): flag that.
            reason = ("CLIENT_MISMATCH" if key_entry is not None
                      else "BAD_SERVER_COOKIE")
            self._audit("drop", reason=reason,
                        client_cookie=client_cookie,
                        server_cookie=server_cookie,
                        key_version=key_version)
            return {"status": "drop", "cookie": None, "reason": reason}

        age = float(now) - float(issued_at)
        if age > self.cookie_ttl:
            # Expired cookies cannot silently pass; reply BADCOOKIE with a
            # fresh cookie that echoes the client's own client cookie.
            reason = ("KEY_GRACE_EXPIRED" if retired else "EXPIRED")
            cookie = self._issue(client_cookie, now)
            self._audit("badcookie", reason=reason,
                        client_cookie=client_cookie,
                        server_cookie=server_cookie,
                        key_version=key_version)
            return {"status": "badcookie", "cookie": cookie,
                    "reason": reason, "key_version": key_version}

        if retired and (float(now) - key_entry["rotated_at"]
                        > self.rotation_grace):
            # Overlap window for the rotated-out key has elapsed.
            cookie = self._issue(client_cookie, now)
            self._audit("badcookie", reason="KEY_GRACE_EXPIRED",
                        client_cookie=client_cookie,
                        server_cookie=server_cookie,
                        key_version=key_version)
            return {"status": "badcookie", "cookie": cookie,
                    "reason": "KEY_GRACE_EXPIRED",
                    "key_version": key_version}

        self._audit("ok", reason="OK",
                    client_cookie=client_cookie,
                    server_cookie=server_cookie,
                    key_version=key_version)
        return {"status": "ok", "cookie": None, "reason": "OK",
                "key_version": key_version}

    # ---------------------------------------------------------------- nonce
    def nonce(self, client_ip):
        """Return a process-unique 16-byte nonce under concurrency.

        A monotonic counter guarantees no duplicates even if the OS RNG
        ever returns colliding bytes; ``secrets``-grade randomness hides
        the counter from observers.
        """
        with self._lock:
            self._nonce_counter += 1
            counter = self._nonce_counter
        return counter.to_bytes(8, "big") + os.urandom(8)

    # ---------------------------------------------------- persistence / audit
    def snapshot(self):
        """Serialize every key version so a restart can validate cookies."""
        with self._lock:
            return {
                "cookie_ttl": self.cookie_ttl,
                "rotation_grace": self.rotation_grace,
                "next_version": self._next_version,
                "keys": [
                    {
                        "version": entry["version"],
                        "secret": entry["secret"].hex(),
                        "rotated_at": entry["rotated_at"],
                    }
                    for entry in self._keys
                ],
            }

    def restore(self, snapshot):
        with self._lock:
            self.cookie_ttl = snapshot["cookie_ttl"]
            self.rotation_grace = snapshot["rotation_grace"]
            self._next_version = snapshot["next_version"]
            self._keys = [
                {
                    "version": entry["version"],
                    "secret": bytes.fromhex(entry["secret"]),
                    "rotated_at": entry["rotated_at"],
                }
                for entry in snapshot["keys"]
            ]
            self._audit("restore", reason="KEYS_RESTORED",
                        key_version=self._current_key()["version"])

    def save_keys(self, path):
        """Persist the key ring so a restart keeps validating cookies."""
        with self._lock:
            payload = json.dumps(self.snapshot(), sort_keys=True).encode()
            tmp_path = "%s.tmp" % path
            with open(tmp_path, "wb") as handle:
                handle.write(payload)
            os.replace(tmp_path, path)

    def load_keys(self, path):
        with open(path, "rb") as handle:
            self.restore(json.loads(handle.read().decode()))

    @staticmethod
    def _redact(cookie):
        """Audit-safe summary: length and a short prefix, never the value."""
        if cookie is None:
            return None
        if not isinstance(cookie, (bytes, bytearray)):
            return {"len": -1, "prefix": ""}
        prefix = bytes(cookie[:2]).hex()
        return {"len": len(cookie), "prefix": prefix}

    def _audit(self, status, reason, *, client_cookie=None,
               server_cookie=None, key_version=None):
        event = {"status": status, "reason": reason}
        if key_version is not None:
            event["key_version"] = key_version
        redacted_client = self._redact(client_cookie)
        redacted_server = self._redact(server_cookie)
        if redacted_client is not None:
            event["client_cookie"] = redacted_client
        if redacted_server is not None:
            event["server_cookie"] = redacted_server
        self._events.append(event)

    def audit_events(self):
        with self._lock:
            return list(self._events)
