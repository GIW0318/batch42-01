"""RTSP session state cache skeleton."""

INVALID_STATE = "invalid-state"
SESSION_IN_USE = "session-in-use"
CHANNEL_CONFLICT = "channel-conflict"
DIGEST_REPLAY = "digest-replay"
INVALID_RANGE = "invalid-range"
RTP_INFO_MISMATCH = "rtp-info-mismatch"
SESSION_LIMIT = "session-limit"


class RtspError(Exception):
    def __init__(self, code, message=""):
        self.code = code
        super().__init__(code if not message else "%s: %s" % (code, message))


class RtspSessionCache:
    """Session state machine for RTSP SETUP/PLAY/TEARDOWN.

    Session headers are normalized to lower case without surrounding
    whitespace.  Expiry uses the cache's monotonic clock advanced through
    advance(); one control connection owns each session.
    """

    def __init__(self, session_timeout=60, max_sessions=100, digest_window=300):
        self.session_timeout = session_timeout
        self.max_sessions = max_sessions
        self.digest_window = digest_window
        self._clock = 0

    def normalize_session_id(self, header_value):
        raise NotImplementedError

    def create_session(self, url, conn_id):
        raise NotImplementedError

    def attach(self, session_id, conn_id):
        raise NotImplementedError

    def setup(self, session_id, conn_id, track, rtp_channel, rtcp_channel):
        raise NotImplementedError

    def play(self, session_id, range_header=None, file_size=None):
        raise NotImplementedError

    def teardown(self, session_id):
        raise NotImplementedError

    def session_state(self, session_id):
        raise NotImplementedError

    def session_count(self):
        raise NotImplementedError

    def authorize_digest(self, session_id, nonce, nc):
        raise NotImplementedError

    def parse_rtp_info(self, session_id, entries):
        raise NotImplementedError

    def rtp_info(self, session_id, track):
        raise NotImplementedError

    def advance(self, seconds):
        raise NotImplementedError

    def expire(self):
        raise NotImplementedError

    def snapshot(self):
        raise NotImplementedError

    def restore(self, snapshot):
        raise NotImplementedError
