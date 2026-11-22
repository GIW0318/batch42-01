"""RTSP session state cache.

Implements the SETUP/PLAY/TEARDOWN state machine for RTSP sessions with:

* normalized Session headers (lower case, surrounding whitespace stripped);
* a single owning control connection per session;
* monotonic-clock based expiry, immune to wall-clock adjustments;
* atomic state transitions (failed SETUP leaves no half-initialized state);
* explicit errors on interleaved channel conflicts;
* digest replay protection keyed on (nonce, nc) with a monotonic time window;
* RTP-Info entries whose sequence number and timestamp are written in pairs;
* restart-safe session identifiers backed by snapshot/restore.
"""

import threading

INVALID_STATE = "invalid-state"
SESSION_IN_USE = "session-in-use"
CHANNEL_CONFLICT = "channel-conflict"
DIGEST_REPLAY = "digest-replay"
INVALID_RANGE = "invalid-range"
RTP_INFO_MISMATCH = "rtp-info-mismatch"
SESSION_LIMIT = "session-limit"

STATE_INIT = "init"
STATE_READY = "ready"
STATE_PLAYING = "playing"
STATE_TEARDOWN = "teardown"

SNAPSHOT_VERSION = 1


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
        self._next_id = 1
        self._sessions = {}
        self._lock = threading.RLock()

    def normalize_session_id(self, header_value):
        if header_value is None:
            raise RtspError(INVALID_STATE, "missing Session header")
        text = str(header_value).strip()
        if ":" in text:
            _, _, text = text.partition(":")
            text = text.strip()
        if ";" in text:
            text = text.split(";", 1)[0].strip()
        return text.lower()

    def create_session(self, url, conn_id):
        with self._lock:
            active = [
                record
                for record in self._sessions.values()
                if record["state"] != STATE_TEARDOWN
            ]
            if len(active) >= self.max_sessions:
                raise RtspError(
                    SESSION_LIMIT,
                    "session limit %d reached" % self.max_sessions,
                )
            session_id = "s%012d" % self._next_id
            self._next_id += 1
            now = self._clock
            self._sessions[session_id] = {
                "id": session_id,
                "url": url,
                "conn_id": conn_id,
                "state": STATE_INIT,
                "tracks": {},
                "channels": set(),
                "rtp_info": {},
                "nonces": {},
                "created_at": now,
                "last_active": now,
            }
            return session_id

    def attach(self, session_id, conn_id):
        with self._lock:
            record = self._get_session(session_id)
            self._require_alive(record)
            if record["conn_id"] != conn_id:
                raise RtspError(
                    SESSION_IN_USE,
                    "session %s owned by another control connection"
                    % record["id"],
                )
            record["last_active"] = self._clock
            return True

    def setup(self, session_id, conn_id, track, rtp_channel, rtcp_channel):
        with self._lock:
            record = self._get_session(session_id)
            self._require_alive(record)
            if record["state"] not in (STATE_INIT, STATE_READY):
                raise RtspError(
                    INVALID_STATE,
                    "SETUP not allowed in state %s" % record["state"],
                )
            if record["conn_id"] != conn_id:
                raise RtspError(
                    SESSION_IN_USE,
                    "session %s owned by another control connection"
                    % record["id"],
                )
            if track in record["tracks"]:
                raise RtspError(
                    INVALID_STATE, "track %s already set up" % track
                )

            candidate_tracks = dict(record["tracks"])
            candidate_channels = set(record["channels"])
            if rtp_channel == rtcp_channel:
                raise RtspError(
                    CHANNEL_CONFLICT,
                    "RTP and RTCP share interleaved channel %d" % rtp_channel,
                )
            for channel in (rtp_channel, rtcp_channel):
                if channel in candidate_channels:
                    raise RtspError(
                        CHANNEL_CONFLICT,
                        "interleaved channel %d already in use" % channel,
                    )

            candidate_tracks[track] = {
                "rtp_channel": rtp_channel,
                "rtcp_channel": rtcp_channel,
            }
            candidate_channels.update((rtp_channel, rtcp_channel))

            record["tracks"] = candidate_tracks
            record["channels"] = candidate_channels
            record["state"] = STATE_READY
            record["last_active"] = self._clock
            return {
                "state": record["state"],
                "track": track,
                "rtp_channel": rtp_channel,
                "rtcp_channel": rtcp_channel,
            }

    def play(self, session_id, range_header=None, file_size=None):
        with self._lock:
            record = self._get_session(session_id)
            self._require_alive(record)
            if record["state"] not in (STATE_READY, STATE_PLAYING):
                raise RtspError(
                    INVALID_STATE,
                    "PLAY requires a SETUP session, state is %s"
                    % record["state"],
                )
            npt_range = self._parse_range(range_header, file_size)
            record["state"] = STATE_PLAYING
            record["last_active"] = self._clock
            return {"state": record["state"], "range": npt_range}

    def teardown(self, session_id):
        with self._lock:
            record = self._get_session(session_id)
            if record["state"] == STATE_TEARDOWN:
                raise RtspError(
                    INVALID_STATE, "session already torn down"
                )
            released = sorted(record["channels"])
            record["channels"] = set()
            record["tracks"] = {}
            record["state"] = STATE_TEARDOWN
            record["last_active"] = self._clock
            return released

    def session_state(self, session_id):
        with self._lock:
            return self._get_session(session_id)["state"]

    def session_count(self):
        with self._lock:
            return sum(
                1
                for record in self._sessions.values()
                if record["state"] != STATE_TEARDOWN
            )

    def authorize_digest(self, session_id, nonce, nc):
        with self._lock:
            record = self._get_session(session_id)
            self._require_alive(record)
            self._prune_nonces(record)
            seen = record["nonces"].setdefault(nonce, {})
            if nc in seen:
                raise RtspError(
                    DIGEST_REPLAY,
                    "nonce %r with nc=%s was already used" % (nonce, nc),
                )
            seen[nc] = self._clock
            record["last_active"] = self._clock
            return True

    def parse_rtp_info(self, session_id, entries):
        with self._lock:
            record = self._get_session(session_id)
            self._require_alive(record)
            candidates = {}
            for entry in entries:
                if (
                    not isinstance(entry, dict)
                    or "track" not in entry
                    or "seq" not in entry
                    or "timestamp" not in entry
                ):
                    raise RtspError(
                        RTP_INFO_MISMATCH,
                        "RTP-Info entry needs paired seq and timestamp: %r"
                        % (entry,),
                    )
                candidates[entry["track"]] = {
                    "seq": entry["seq"],
                    "timestamp": entry["timestamp"],
                }
            record["rtp_info"].update(candidates)
            record["last_active"] = self._clock
            return {
                track: dict(info) for track, info in record["rtp_info"].items()
            }

    def rtp_info(self, session_id, track):
        with self._lock:
            record = self._get_session(session_id)
            if track not in record["rtp_info"]:
                raise RtspError(
                    RTP_INFO_MISMATCH,
                    "no RTP-Info stored for track %s" % track,
                )
            return dict(record["rtp_info"][track])

    def advance(self, seconds):
        with self._lock:
            if seconds < 0:
                raise ValueError("monotonic clock cannot move backwards")
            self._clock += seconds
            return self._clock

    def expire(self):
        expired = []
        with self._lock:
            for session_id in list(self._sessions):
                record = self._sessions[session_id]
                if record["state"] == STATE_TEARDOWN:
                    continue
                if self._clock - record["last_active"] > self.session_timeout:
                    expired.append(session_id)
                    del self._sessions[session_id]
        return expired

    def snapshot(self):
        with self._lock:
            return {
                "version": SNAPSHOT_VERSION,
                "next_id": self._next_id,
                "clock": self._clock,
                "sessions": [
                    {
                        "id": record["id"],
                        "url": record["url"],
                        "conn_id": record["conn_id"],
                        "state": record["state"],
                        "tracks": {
                            track: dict(info)
                            for track, info in record["tracks"].items()
                        },
                        "channels": sorted(record["channels"]),
                        "rtp_info": {
                            track: dict(info)
                            for track, info in record["rtp_info"].items()
                        },
                        "nonces": {
                            nonce: dict(seen)
                            for nonce, seen in record["nonces"].items()
                        },
                        "created_at": record["created_at"],
                        "last_active": record["last_active"],
                    }
                    for record in self._sessions.values()
                ],
            }

    def restore(self, snapshot):
        with self._lock:
            sessions = {}
            for item in snapshot.get("sessions", []):
                record = dict(item)
                record["channels"] = set(item.get("channels", ()))
                record["tracks"] = {
                    track: dict(info) for track, info in item.get("tracks", {}).items()
                }
                record["rtp_info"] = {
                    track: dict(info)
                    for track, info in item.get("rtp_info", {}).items()
                }
                record["nonces"] = {
                    nonce: dict(seen)
                    for nonce, seen in item.get("nonces", {}).items()
                }
                sessions[record["id"]] = record
            self._sessions = sessions
            self._next_id = snapshot.get("next_id", 1)
            self._clock = snapshot.get("clock", 0)

    def _get_session(self, session_id):
        key = self.normalize_session_id(session_id)
        record = self._sessions.get(key)
        if record is None:
            raise RtspError(
                INVALID_STATE, "unknown session %r" % (session_id,)
            )
        return record

    def _require_alive(self, record):
        if record["state"] == STATE_TEARDOWN:
            raise RtspError(
                INVALID_STATE, "session %s already torn down" % record["id"]
            )

    def _prune_nonces(self, record):
        for nonce in list(record["nonces"]):
            seen = record["nonces"][nonce]
            for nc in list(seen):
                if self._clock - seen[nc] > self.digest_window:
                    del seen[nc]
            if not seen:
                del record["nonces"][nonce]

    def _parse_range(self, range_header, file_size):
        if range_header is None:
            return None
        text = str(range_header).strip().lower()
        if text.startswith("npt="):
            text = text[len("npt="):]
        if "-" not in text:
            raise RtspError(INVALID_RANGE, "malformed Range: %r" % range_header)
        start_text, _, end_text = text.partition("-")
        try:
            start = self._to_number(start_text) if start_text.strip() else 0
            end = self._to_number(end_text) if end_text.strip() else None
        except ValueError:
            raise RtspError(
                INVALID_RANGE, "malformed Range: %r" % range_header
            )
        if file_size is not None:
            limit = self._to_number(file_size)
            if end is None:
                end = limit
            if start < 0 or end < start or start > limit or end > limit:
                raise RtspError(
                    INVALID_RANGE,
                    "range %s-%s exceeds file size %s" % (start, end, limit),
                )
        if end is None:
            end = start
        return (start, end)

    @staticmethod
    def _to_number(value):
        if isinstance(value, bool):
            return int(value)
        if isinstance(value, int):
            return value
        text = str(value).strip()
        number = float(text)
        if number.is_integer():
            return int(number)
        return number
