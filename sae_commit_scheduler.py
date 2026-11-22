"""WPA3 SAE commit/confirm scheduler skeleton."""

ANTI_CLOGGING = "anti-clogging"
TOKEN_MISMATCH = "token-mismatch"
GROUP_NOT_ALLOWED = "group-not-allowed"
INVALID_LENGTH = "invalid-length"
CONFIRM_MISMATCH = "confirm-mismatch"
AUTHENTICATION_FAILED = "authentication-failed"
INVALID_STATE = "invalid-state"
STA_BUSY = "sta-busy"


class SaeError(Exception):
    def __init__(self, code, message="", field=None):
        self.code = code
        self.field = field
        super().__init__(code if not message else "%s: %s" % (code, message))


class SaeCommitScheduler:
    """Per-STA SAE handshake state with anti-clogging tokens.

    Commit scalars are 32 bytes and elements 64 bytes.  A Commit without a
    token is rejected before any group arithmetic, retransmissions are
    idempotent per (STA, token) and key installation happens together with
    the transition to authenticated state.
    """

    def __init__(self, allowed_groups=(19, 20), commit_timeout=30,
                 credentials=None, pmksa_ttl=3600):
        self.allowed_groups = tuple(allowed_groups)
        self.commit_timeout = commit_timeout
        self.credentials = dict(credentials or {})
        self.pmksa_ttl = pmksa_ttl
        self._clock = 0

    def issue_token(self, sta):
        raise NotImplementedError

    def commit(self, sta, token, group=19, scalar=b"", element=b"",
               password=None, ssid=None):
        raise NotImplementedError

    def confirm(self, sta, send_id, recv_id, confirm=b""):
        raise NotImplementedError

    def session_state(self, sta):
        raise NotImplementedError

    def current_group(self, sta):
        raise NotImplementedError

    def key_install_count(self, sta):
        raise NotImplementedError

    def advance(self, seconds):
        raise NotImplementedError

    def expire(self):
        raise NotImplementedError

    def cache_pmksa(self, sta, ssid, pmkid, pmk):
        raise NotImplementedError

    def lookup_pmksa(self, sta, ssid, pmkid):
        raise NotImplementedError

    def snapshot(self):
        raise NotImplementedError

    def restore(self, snapshot):
        raise NotImplementedError
