"""NTLM relay fencing.

Every NTLM exchange is pinned to the connection that started it: the Type 2
challenge is single use, the Type 3 message must carry a valid MIC and a
channel binding matching the connection's TLS channel, and the TargetInfo SPN
must match the service the client actually connected to.  Downgrade and
fallback paths (NTLMv1, anonymous, guest, cross-domain referrals) are refused.
"""

import secrets

MIC_REQUIRED = "mic-required"
CBT_MISMATCH = "cbt-mismatch"
SPN_MISMATCH = "spn-mismatch"
NTLMV1_REJECTED = "ntlmv1-rejected"
CHALLENGE_REUSE = "challenge-reuse"
ANONYMOUS_REJECTED = "anonymous-rejected"
GUEST_REJECTED = "guest-rejected"
CROSS_DOMAIN = "cross-domain-referral"
CONNECTION_CLOSED = "connection-closed"
UNKNOWN_CONNECTION = "unknown-connection"

STATE_CHALLENGING = "challenging"
STATE_AUTHENTICATED = "authenticated"
STATE_CLOSED = "closed"
STATE_UNKNOWN = "unknown"

ANONYMOUS_USERS = frozenset(("", "anonymous"))
GUEST_USERS = frozenset(("guest",))


class NtlmError(Exception):
    def __init__(self, code, message=""):
        self.code = code
        super().__init__(code if not message else "%s: %s" % (code, message))


class NtlmRelayFence:
    """Requires MIC and channel binding together before accepting NTLM.

    A challenge is issued per connection, tied to the connection's channel
    binding and target SPN, and consumed exactly once.  Failures close the
    connection and are audited with source address and target SPN.
    """

    def __init__(self, allowed_domains=("CORP",), require_mic=True,
                 require_cbt=True):
        self.allowed_domains = tuple(domain.upper() for domain in allowed_domains)
        self.require_mic = require_mic
        self.require_cbt = require_cbt
        self._connections = {}
        self._audit = []

    def begin(self, conn_id, source_ip, target_spn, channel_binding):
        """Start a fresh NTLM negotiation on ``conn_id``.

        Re-negotiating on an existing connection resets its state and issues
        a new single-use challenge, so any response to an older challenge is
        rejected as a replay.
        """
        challenge = secrets.token_hex(16)
        self._connections[conn_id] = {
            "state": STATE_CHALLENGING,
            "source_ip": source_ip,
            "target_spn": target_spn,
            "channel_binding": channel_binding,
            "challenge": challenge,
            "user": None,
        }
        return {"challenge": challenge}

    def authenticate(self, conn_id, message):
        connection = self._connections.get(conn_id)
        if connection is None:
            raise NtlmError(UNKNOWN_CONNECTION)
        if connection["state"] == STATE_CLOSED:
            raise NtlmError(CONNECTION_CLOSED)

        if connection["state"] == STATE_AUTHENTICATED:
            if message.get("challenge") == connection["challenge"]:
                return {
                    "authenticated": True,
                    "duplicate": True,
                    "user": connection["user"],
                }
            self._reject_replay(connection, CHALLENGE_REUSE, message)

        # Anti-replay gate: the Type 3 response must answer the challenge
        # issued to this exact connection.  A stale/foreign challenge never
        # reaches credential evaluation and does not consume the pending one.
        if message.get("challenge") != connection["challenge"]:
            self._reject_replay(connection, CHALLENGE_REUSE, message)

        if message.get("ntlm_version") != 2:
            self._deny(connection, NTLMV1_REJECTED, message)

        username = str(message.get("user", "")).strip().lower()
        if username in ANONYMOUS_USERS:
            self._deny(connection, ANONYMOUS_REJECTED, message)
        if username in GUEST_USERS:
            self._deny(connection, GUEST_REJECTED, message)

        domain = str(message.get("domain", "")).upper()
        if domain not in self.allowed_domains:
            self._deny(connection, CROSS_DOMAIN, message)

        if self.require_mic and not message.get("mic_valid"):
            self._deny(connection, MIC_REQUIRED, message)

        if self.require_cbt:
            if message.get("cbt") != connection["channel_binding"]:
                self._deny(connection, CBT_MISMATCH, message)

        if message.get("spn") != connection["target_spn"]:
            self._deny(connection, SPN_MISMATCH, message)

        identity = "%s\\%s" % (domain, str(message.get("user", "")).strip())
        connection["state"] = STATE_AUTHENTICATED
        connection["user"] = identity
        self._audit.append({
            "source_ip": connection["source_ip"],
            "target_spn": connection["target_spn"],
            "user": identity,
            "decision": "authenticated",
        })
        return {
            "authenticated": True,
            "duplicate": False,
            "user": identity,
        }

    def close(self, conn_id):
        connection = self._connections.get(conn_id)
        if connection is not None:
            connection["state"] = STATE_CLOSED

    def connection_state(self, conn_id):
        connection = self._connections.get(conn_id)
        if connection is None:
            return STATE_UNKNOWN
        return connection["state"]

    def audit_log(self):
        return list(self._audit)

    def _reject_replay(self, connection, code, message):
        """Reject a replayed/stale challenge without poisoning the exchange."""
        self._audit.append({
            "source_ip": connection["source_ip"],
            "target_spn": connection["target_spn"],
            "user": message.get("user"),
            "decision": "denied",
            "reason": code,
        })
        raise NtlmError(code)

    def _deny(self, connection, code, message):
        """Fail the authentication and shut the connection down."""
        connection["state"] = STATE_CLOSED
        connection["user"] = None
        self._audit.append({
            "source_ip": connection["source_ip"],
            "target_spn": connection["target_spn"],
            "user": message.get("user"),
            "decision": "denied",
            "reason": code,
        })
        raise NtlmError(code)
