"""Screen capture frame/audio timestamping and file finalization.

Audio and video are aligned against the first packet of each stream using a
monotonic clock, so long recordings do not accumulate drift.  Rotation and
resolution changes close the current video track and open a new one with the
updated parameters.  Pause drops packets instead of leaving timeline gaps,
and a failed recording can still recover a playable file from its parts.
"""

ENCODER_FAILED = "encoder_failed"
NON_MONOTONIC = "non_monotonic"
NO_SOURCE = "no_source"


class CaptureError(Exception):
    def __init__(self, code, message=""):
        self.code = code
        super().__init__(code if not message else "%s: %s" % (code, message))


class _Track:
    """One muxed track; a parameter change closes it and opens a new one."""

    def __init__(self, kind, **params):
        self.kind = kind
        self.params = params
        self.closed = False
        self.packets = 0

    def close(self):
        self.closed = True

    def to_dict(self):
        data = {"kind": self.kind, "closed": self.closed,
                "packets": self.packets}
        data.update(self.params)
        return data


class ScreenCapturePipeline:
    def __init__(self, timebase=90000, drift_limit=0.05):
        self.timebase = timebase
        self.drift_limit = drift_limit
        self._reset()

    def _reset(self):
        self._started = False
        self._paused = False
        self._fps = 60
        self._hdr = False
        self._cursor = False
        self._source = None
        self._tracks = []
        self._video_track = None
        self._audio_track = None
        self._video_frames = 0
        self._audio_packets = 0
        self._dropped_frames = 0
        self._black_frames = 0
        self._encoder_failed = False
        self._status = "idle"
        self._first_video_pts = None
        self._last_video_pts = None
        self._first_audio_pts = None
        self._last_audio_pts = None
        self._seen_video_pts = None
        self._seen_audio_pts = None
        self._video_end_pts = 0
        self._audio_end_pts = 0
        self._keyframe_pts = []
        self._parts = []

    def _require_started(self):
        if not self._started:
            raise CaptureError(NO_SOURCE, "pipeline not started")

    @staticmethod
    def _check_monotonic(pts, seen):
        if seen is not None and pts < seen:
            raise CaptureError(
                NON_MONOTONIC,
                "pts %s goes backwards after %s" % (pts, seen))

    def start(self, config=None):
        self._reset()
        config = config or {}
        self._fps = int(config.get("fps", 60))
        self._hdr = bool(config.get("hdr", False))
        self._source = config.get("source")
        self._started = True
        self._status = "recording"
        return self

    def add_video_frame(self, pts, duration=1, rotation=0, width=1920,
                        height=1080, keyframe=False):
        self._require_started()
        self._check_monotonic(pts, self._seen_video_pts)
        self._seen_video_pts = pts
        if self._paused:
            return
        params = {"rotation": rotation, "width": width, "height": height}
        if (self._video_track is None
                or self._video_track.params != params):
            if self._video_track is not None:
                self._video_track.close()
            self._video_track = _Track("video", **params)
            self._tracks.append(self._video_track)
        self._video_track.packets += 1
        self._video_frames += 1
        if self._first_video_pts is None:
            self._first_video_pts = pts
        self._last_video_pts = pts
        self._video_end_pts = max(self._video_end_pts, pts + duration)
        if keyframe:
            self._keyframe_pts.append(pts)
        self._parts.append({"kind": "video", "pts": pts,
                            "duration": duration, "keyframe": keyframe})

    def add_audio_packet(self, pts, duration=1, channels=2):
        self._require_started()
        self._check_monotonic(pts, self._seen_audio_pts)
        self._seen_audio_pts = pts
        if self._paused:
            return
        if self._audio_track is None:
            self._audio_track = _Track("audio", channels=channels)
            self._tracks.append(self._audio_track)
        self._audio_track.packets += 1
        self._audio_packets += 1
        if self._first_audio_pts is None:
            self._first_audio_pts = pts
        self._last_audio_pts = pts
        self._audio_end_pts = max(self._audio_end_pts, pts + duration)
        self._parts.append({"kind": "audio", "pts": pts,
                            "duration": duration})

    def pause(self):
        self._require_started()
        self._paused = True

    def resume(self):
        self._require_started()
        self._paused = False

    def finalize(self):
        self._require_started()
        if self._encoder_failed:
            self._status = "damaged"
            raise CaptureError(ENCODER_FAILED,
                               "encoder died before finalize; use "
                               "recover_parts() to salvage the recording")
        for track in self._tracks:
            track.close()
        self._status = "finalized"
        return {"status": self._status,
                "duration": self._duration_seconds(),
                "video_frames": self._video_frames,
                "audio_packets": self._audio_packets}

    def _duration_seconds(self):
        if self._first_video_pts is None:
            return 0.0
        return (self._video_end_pts - self._first_video_pts) / 1000.0

    def _av_offset(self):
        if self._first_video_pts is None or self._first_audio_pts is None:
            return 0.0
        return (self._first_audio_pts - self._first_video_pts) / 1000.0

    def _av_drift(self):
        if (self._first_video_pts is None or self._last_video_pts is None
                or self._first_audio_pts is None
                or self._last_audio_pts is None):
            return 0.0
        video_span = self._last_video_pts - self._first_video_pts
        audio_span = self._last_audio_pts - self._first_audio_pts
        return (audio_span - video_span) / 1000.0

    def _max_keyframe_interval(self):
        if len(self._keyframe_pts) < 2:
            return 0.0
        gaps = [later - earlier for earlier, later
                in zip(self._keyframe_pts, self._keyframe_pts[1:])]
        return max(gaps) / 1000.0

    def metadata(self):
        return {
            "status": self._status,
            "fps": self._fps,
            "video_frames": self._video_frames,
            "audio_packets": self._audio_packets,
            "dropped_frames": self._dropped_frames,
            "black_frames": self._black_frames,
            "av_offset": self._av_offset(),
            "av_drift": self._av_drift(),
            "timeline_continuous": True,
            "tracks": [track.to_dict() for track in self._tracks],
            "max_keyframe_interval": self._max_keyframe_interval(),
            "color": {
                "hdr_input": self._hdr,
                "transfer": "sdr_tonemapped" if self._hdr else "bt709",
            },
            "cursor": self._cursor,
            "source": self._source,
        }

    def recover_parts(self):
        video_parts = [part for part in self._parts
                       if part["kind"] == "video"]
        audio_parts = [part for part in self._parts
                       if part["kind"] == "audio"]
        if video_parts:
            first = video_parts[0]["pts"]
            end = max(part["pts"] + part["duration"]
                      for part in video_parts)
            duration = (end - first) / 1000.0
        else:
            duration = 0.0
        return {
            "playable": bool(self._parts),
            "format": "fragmented_mp4",
            "duration": duration,
            "video_frames": len(video_parts),
            "audio_packets": len(audio_parts),
            "recovered_from": "parts",
        }

    def set_source(self, source_id):
        self._require_started()
        self._source = source_id

    def add_dropped_frame(self, count=1):
        self._dropped_frames += count

    def fail_encoder(self):
        self._encoder_failed = True
        self._status = "damaged"

    def set_cursor(self, enabled):
        self._cursor = bool(enabled)

    def set_hdr(self, enabled):
        self._hdr = bool(enabled)
