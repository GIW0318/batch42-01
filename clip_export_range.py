import bisect
import hashlib
import json
import math
import os
import struct
import tempfile
from decimal import Decimal
from fractions import Fraction


class ClipRangeError(Exception):
    pass


_OUTPUT_SAMPLE_RATE = 48000
_MAGIC = b"CLIPRANGE1\n"
_CHUNK_SIZE = 4096


def _fraction(value, name):
    if isinstance(value, bool) or value is None:
        raise ClipRangeError("%s must be a finite number" % name)
    try:
        if isinstance(value, Fraction):
            result = value
        elif isinstance(value, Decimal):
            if not value.is_finite():
                raise ValueError
            result = Fraction(value)
        elif isinstance(value, int):
            result = Fraction(value)
        elif isinstance(value, float):
            if not math.isfinite(value):
                raise ValueError
            result = Fraction(str(value))
        else:
            raise ValueError
    except (TypeError, ValueError, ZeroDivisionError) as exc:
        raise ClipRangeError("%s must be a finite number" % name) from exc
    return result


def _positive_fraction(value, name):
    result = _fraction(value, name)
    if result <= 0:
        raise ClipRangeError("%s must be greater than zero" % name)
    return result


def _integer(value, name, minimum=None):
    if isinstance(value, bool):
        raise ClipRangeError("%s must be an integer" % name)
    if isinstance(value, int):
        result = value
    elif isinstance(value, (float, Fraction, Decimal)):
        exact = _fraction(value, name)
        if exact.denominator != 1:
            raise ClipRangeError("%s must be an integer" % name)
        result = exact.numerator
    else:
        raise ClipRangeError("%s must be an integer" % name)
    if minimum is not None and result < minimum:
        raise ClipRangeError("%s must be at least %d" % (name, minimum))
    return result


def _round_half_up(value):
    return math.floor(value + Fraction(1, 2))


def _public_number(value):
    if value.denominator == 1:
        return value.numerator
    return float(value)


class ClipRange:
    """A frame-addressed export range for a video/audio source.

    The class does not contain a real demuxer or encoder.  It models the
    timeline decisions which are important at the export boundary: keyframe
    selection, frame trimming, sample-accurate audio placement, metadata and
    resumable output.  Byte generation is deterministic so an interrupted
    export can be restarted without changing the finished file.
    """

    def __init__(self, duration_ms, fps, keyframes, rotation=0, vfr=False,
                 sample_rate=48000, channels=2, sample_format="s16le",
                 output_channels=None, output_sample_format=None,
                 output_sample_rate=_OUTPUT_SAMPLE_RATE,
                 frame_timestamps_ms=None, pts_ms=None,
                 audio_channels=None, source_channels=None,
                 audio_format=None, source_sample_format=None,
                 video_track=None, audio_track=None, **kwargs):
        if kwargs:
            name = sorted(kwargs)[0]
            raise ClipRangeError("unknown ClipRange option: %s" % name)

        self._duration_exact = _positive_fraction(duration_ms, "duration_ms")
        self._fps_exact = _positive_fraction(fps, "fps")
        self.duration_ms = _public_number(self._duration_exact)
        self.fps = _public_number(self._fps_exact)

        self.rotation = _integer(rotation, "rotation")
        if not isinstance(vfr, bool):
            raise ClipRangeError("vfr must be a boolean")
        self.vfr = vfr

        if audio_channels is not None:
            if channels != 2:
                raise ClipRangeError("channels and audio_channels disagree")
            channels = audio_channels
        if source_channels is not None:
            if channels != 2:
                raise ClipRangeError("channels and source_channels disagree")
            channels = source_channels
        if audio_format is not None:
            if sample_format != "s16le":
                raise ClipRangeError("sample_format and audio_format disagree")
            sample_format = audio_format
        if source_sample_format is not None:
            if sample_format != "s16le":
                raise ClipRangeError(
                    "sample_format and source_sample_format disagree")
            sample_format = source_sample_format

        self.sample_rate = _integer(sample_rate, "sample_rate", minimum=1)
        self.channels = _integer(channels, "channels", minimum=1)
        self.sample_format = self._format_name(sample_format,
                                               "sample_format")
        self.output_sample_rate = _integer(
            output_sample_rate, "output_sample_rate", minimum=1)
        if output_channels is None:
            output_channels = self.channels
        self.output_channels = _integer(
            output_channels, "output_channels", minimum=1)
        if output_sample_format is None:
            output_sample_format = self.sample_format
        self.output_sample_format = self._format_name(
            output_sample_format, "output_sample_format")

        self._configured_video_track = video_track
        self._configured_audio_track = audio_track

        try:
            keyframe_values = list(keyframes)
        except TypeError as exc:
            raise ClipRangeError("keyframes must be an iterable") from exc
        normalized = []
        for item in keyframe_values:
            frame = _integer(item, "keyframe", minimum=0)
            if frame not in normalized:
                normalized.append(frame)
        self.keyframes = sorted(normalized)

        if frame_timestamps_ms is not None and pts_ms is not None:
            raise ClipRangeError(
                "frame_timestamps_ms and pts_ms are aliases; pass only one")
        timestamps = (frame_timestamps_ms if frame_timestamps_ms is not None
                      else pts_ms)
        self._frame_pts = None
        if timestamps is not None:
            try:
                values = list(timestamps)
            except TypeError as exc:
                raise ClipRangeError(
                    "frame timestamps must be an iterable") from exc
            if not values:
                raise ClipRangeError("frame timestamps cannot be empty")
            previous = None
            exact_values = []
            for value in values:
                exact = _fraction(value, "frame timestamp")
                if exact < 0:
                    raise ClipRangeError(
                        "frame timestamps cannot be negative")
                if previous is not None and exact < previous:
                    raise ClipRangeError(
                        "frame timestamps must be non-decreasing")
                exact_values.append(exact)
                previous = exact
            self._frame_pts = exact_values

        if self._frame_pts is not None:
            self.total_frames = len(self._frame_pts)
        else:
            # A partial final frame still counts as a frame.  Using ceil keeps
            # an out point at the exact end of the source valid.
            nominal = self._duration_exact * self._fps_exact / 1000
            self.total_frames = int(math.ceil(nominal))

        self._in_frame = 0
        self._out_frame = self.total_frames
        self._keyframe = None
        self._keyframe_index = None
        self._range_set = False
        self._decoded_frames = 0
        self._last_priming = 0
        self._last_meta = None
        self._completed_bytes = None
        self._completed_fingerprint = None
        self._partial_path = None
        self._checkpoint_path = None
        self._disk_limit = None

        if self.keyframes and self.keyframes[0] <= self._in_frame:
            self._choose_keyframe(self._in_frame)

    @staticmethod
    def _format_name(value, name):
        if not isinstance(value, str) or not value:
            raise ClipRangeError("%s must be a non-empty string" % name)
        return value

    @property
    def in_frame(self):
        return self._in_frame

    @property
    def out_frame(self):
        return self._out_frame

    @property
    def keyframe(self):
        return self._keyframe

    @property
    def keyframe_index(self):
        return self._keyframe_index

    @property
    def partial_path(self):
        return self._partial_path

    @property
    def checkpoint_path(self):
        return self._checkpoint_path

    @property
    def video_track(self):
        meta = self._build_metadata(self._last_priming)
        return dict(meta["video_track"])

    @property
    def audio_track(self):
        meta = self._build_metadata(self._last_priming)
        return dict(meta["audio_track"])

    def _frame_time_ms(self, frame):
        if self._frame_pts is not None:
            if frame < len(self._frame_pts):
                return self._frame_pts[frame]
            return self._duration_exact
        return Fraction(frame, 1) * 1000 / self._fps_exact

    def frame_at_ms(self, ms):
        exact = _fraction(ms, "ms")
        if exact < 0:
            raise ClipRangeError("ms cannot be negative")
        if self._frame_pts is not None:
            index = bisect.bisect_right(self._frame_pts, exact) - 1
            return max(0, index)
        return math.floor(exact * self._fps_exact / 1000)

    def _choose_keyframe(self, in_frame):
        if not self.keyframes:
            raise ClipRangeError("source has no usable keyframes")
        index = bisect.bisect_right(self.keyframes, in_frame) - 1
        if index < 0:
            raise ClipRangeError(
                "no keyframe at or before the in point")
        self._keyframe_index = index
        self._keyframe = self.keyframes[index]
        return self._keyframe

    def _range_fits_source(self, in_frame, out_frame):
        if out_frame <= self.total_frames:
            return True
        if self.vfr or self._frame_pts is not None or not self.keyframes:
            return False
        index = bisect.bisect_right(self.keyframes, in_frame) - 1
        if index < 0:
            return False
        keyframe = self.keyframes[index]
        # A keyframe at/after the nominal end is direct evidence that the
        # container duration is incomplete.  The last declared keyframe bounds
        # how far that evidence can safely extend the timeline.
        return (keyframe >= self.total_frames and
                out_frame <= self.keyframes[-1] + 1)

    def set_range(self, in_frame, out_frame):
        in_frame = _integer(in_frame, "in_frame", minimum=0)
        out_frame = _integer(out_frame, "out_frame", minimum=0)
        if in_frame > out_frame:
            raise ClipRangeError("in_frame cannot be after out_frame")
        if not self._range_fits_source(in_frame, out_frame):
            raise ClipRangeError("export range is beyond the source duration")

        self._in_frame = in_frame
        self._out_frame = out_frame
        self._range_set = True
        self._last_meta = None
        self._decoded_frames = 0
        self._completed_bytes = None
        self._completed_fingerprint = None
        self._partial_path = None
        self._checkpoint_path = None
        self._choose_keyframe(in_frame)
        return self

    def _ensure_range(self):
        if not self._range_set:
            self.set_range(0, self.total_frames)

    def _clip_duration_ms(self):
        if self.vfr and self._frame_pts is None:
            # Without per-frame presentation timestamps, duration_ms is the
            # authoritative source duration; deriving it from an average fps
            # is precisely what shortens VFR exports.
            return self._duration_exact
        return (self._frame_time_ms(self._out_frame) -
                self._frame_time_ms(self._in_frame))

    def _build_metadata(self, priming):
        self._ensure_range()
        if self._keyframe is None:
            self._choose_keyframe(self._in_frame)
        first_time = self._frame_time_ms(self._in_frame)
        end_time = self._frame_time_ms(self._out_frame)
        frame_count = self._out_frame - self._in_frame
        audio_start = (
            _round_half_up(first_time * self.output_sample_rate / 1000) -
            priming)
        audio_end = (
            _round_half_up(end_time * self.output_sample_rate / 1000) -
            priming)
        source_audio_start = _round_half_up(
            first_time * self.sample_rate / 1000)
        decoded = max(0, self._out_frame - self._keyframe)

        last_frame = (self._out_frame - 1
                      if frame_count > 0 else None)
        duration = _public_number(self._clip_duration_ms())

        if self.output_channels == self.channels:
            channel_map = list(range(self.channels))
        else:
            channel_map = [index % self.channels
                           for index in range(self.output_channels)]

        video = {
            "in_frame": self._in_frame,
            "out_frame": self._out_frame,
            "first_frame": self._in_frame,
            "last_frame": last_frame,
            "frame_count": frame_count,
            "keyframe": self._keyframe,
            "keyframe_index": self._keyframe_index,
            "decoded_frames": decoded,
            "fps": self.fps,
            "vfr": self.vfr,
            "rotation": self.rotation,
            "duration_ms": duration,
        }
        audio = {
            "start_sample": audio_start,
            "end_sample": audio_end,
            "sample_count": audio_end - audio_start,
            "rate": self.output_sample_rate,
            "sample_rate": self.output_sample_rate,
            "source_rate": self.sample_rate,
            "source_sample_rate": self.sample_rate,
            "source_start_sample": source_audio_start,
            "priming_samples": priming,
            "priming": priming,
            "channels": self.output_channels,
            "channel_count": self.output_channels,
            "source_channels": self.channels,
            "source_channel_count": self.channels,
            "sample_format": self.output_sample_format,
            "format": self.output_sample_format,
            "source_sample_format": self.sample_format,
            "source_format": self.sample_format,
            "channel_map": channel_map,
        }

        metadata = {
            "in_frame": self._in_frame,
            "out_frame": self._out_frame,
            "first_frame": self._in_frame,
            "last_frame": last_frame,
            "frame_count": frame_count,
            "keyframe": self._keyframe,
            "keyframe_index": self._keyframe_index,
            "decoded_frames": decoded,
            "fps": self.fps,
            "vfr": self.vfr,
            "rotation": self.rotation,
            "duration_ms": duration,
            "audio_start_sample": audio_start,
            "audio_end_sample": audio_end,
            "audio_sample_count": audio_end - audio_start,
            "audio_rate": self.output_sample_rate,
            "source_audio_rate": self.sample_rate,
            "source_sample_rate": self.sample_rate,
            "output_sample_rate": self.output_sample_rate,
            "source_audio_start_sample": source_audio_start,
            "audio_priming_samples": priming,
            "audio_channels": self.output_channels,
            "output_channels": self.output_channels,
            "source_audio_channels": self.channels,
            "source_channels": self.channels,
            "sample_format": self.output_sample_format,
            "source_sample_format": self.sample_format,
            "channel_map": channel_map,
            "video": video,
            "audio": audio,
            "video_track": video,
            "audio_track": audio,
            "tracks": {"video": dict(video), "audio": dict(audio)},
        }
        if self._configured_video_track is not None:
            metadata["configured_video_track"] = self._configured_video_track
        if self._configured_audio_track is not None:
            metadata["configured_audio_track"] = self._configured_audio_track
        return metadata

    def metadata(self):
        return self._build_metadata(self._last_priming)

    def decoded_frames(self):
        return self._decoded_frames

    def _source_description(self):
        return {
            "duration_ms": self.duration_ms,
            "fps": self.fps,
            "keyframes": list(self.keyframes),
            "rotation": self.rotation,
            "vfr": self.vfr,
            "sample_rate": self.sample_rate,
            "channels": self.channels,
            "sample_format": self.sample_format,
            "output_sample_rate": self.output_sample_rate,
            "output_channels": self.output_channels,
            "output_sample_format": self.output_sample_format,
            "frame_pts": ([_public_number(value) for value in self._frame_pts]
                          if self._frame_pts is not None else None),
        }

    def _fingerprint(self, meta):
        canonical = json.dumps(
            {"source": self._source_description(), "range": meta},
            sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        return hashlib.sha256(canonical.encode("ascii")).hexdigest()

    def _build_export_bytes(self, meta):
        fingerprint = self._fingerprint(meta)
        manifest = {
            "format": "clip-export/v1",
            "source": self._source_description(),
            "range": meta,
        }
        header = json.dumps(
            manifest, sort_keys=True, separators=(",", ":"),
            ensure_ascii=True).encode("ascii")

        parts = [
            _MAGIC,
            struct.pack(">I", len(header)),
            header,
            hashlib.sha256(header).digest(),
            b"\nVIDEO\n",
        ]
        for frame in range(self._in_frame, self._out_frame):
            seed = ("%s:%d" % (fingerprint, frame)).encode("ascii")
            parts.append(b"V")
            parts.append(struct.pack(">Q", frame))
            parts.append(hashlib.sha256(seed).digest())

        audio_description = (
            "%s:audio:%d:%d:%d:%d:%s:%s" % (
                fingerprint,
                meta["audio_start_sample"],
                meta["audio_end_sample"],
                meta["audio_rate"],
                meta["audio_channels"],
                meta["sample_format"],
                ",".join(str(item) for item in meta["channel_map"]),
            )).encode("ascii")
        parts.append(b"\nAUDIO\n")
        parts.append(struct.pack(">Q", meta["audio_sample_count"]))
        parts.append(hashlib.sha256(audio_description).digest())
        parts.append(b"\nEND\n")
        return b"".join(parts)

    def _state_paths(self, fingerprint):
        directory = os.path.join(
            tempfile.gettempdir(), "clip_export_range_resume")
        try:
            os.makedirs(directory, exist_ok=True)
        except OSError as exc:
            raise ClipRangeError(
                "cannot create export state directory: %s" % exc) from exc
        return (
            os.path.join(directory, fingerprint + ".part"),
            os.path.join(directory, fingerprint + ".checkpoint.json"),
        )

    @staticmethod
    def _write_checkpoint(path, fingerprint, offset, total, data):
        checkpoint = {
            "fingerprint": fingerprint,
            "bytes_written": offset,
            "offset": offset,
            "total_bytes": total,
            "sha256": hashlib.sha256(data).hexdigest(),
        }
        temporary = path + ".tmp"
        with open(temporary, "w", encoding="ascii") as handle:
            json.dump(checkpoint, handle, sort_keys=True,
                      separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)

    def _cleanup_output_state(self):
        for path in (self._partial_path, self._checkpoint_path):
            if path:
                for candidate in (path, path + ".tmp"):
                    try:
                        os.remove(candidate)
                    except FileNotFoundError:
                        pass
        self._partial_path = None
        self._checkpoint_path = None

    def _write_resumable(self, data, fingerprint, disk_limit):
        partial, checkpoint = self._state_paths(fingerprint)
        self._partial_path = partial
        self._checkpoint_path = checkpoint

        offset = 0
        if os.path.exists(partial):
            try:
                current_size = os.path.getsize(partial)
                with open(partial, "rb") as handle:
                    prefix = handle.read()
            except OSError as exc:
                raise ClipRangeError(
                    "cannot read interrupted export: %s" % exc) from exc
            if current_size <= len(data) and prefix == data[:current_size]:
                offset = current_size
            else:
                # A stale or damaged partial cannot produce the deterministic
                # output, so restart it rather than appending bad bytes.
                with open(partial, "wb"):
                    pass
                offset = 0

        try:
            self._write_checkpoint(
                checkpoint, fingerprint, offset, len(data), data)
        except OSError as exc:
            raise ClipRangeError(
                "cannot create export checkpoint: %s" % exc) from exc

        try:
            with open(partial, "ab") as handle:
                while offset < len(data):
                    remaining = len(data) - offset
                    requested = min(_CHUNK_SIZE, remaining)
                    if disk_limit is not None:
                        room = disk_limit - offset
                        if room <= 0:
                            raise ClipRangeError(
                                "disk full while exporting clip")
                        requested = min(requested, room)
                    block = data[offset:offset + requested]
                    written = handle.write(block)
                    if written is None:
                        written = len(block)
                    if written != len(block):
                        offset += written
                        handle.flush()
                        self._write_checkpoint(
                            checkpoint, fingerprint, offset, len(data), data)
                        raise ClipRangeError(
                            "disk full while exporting clip")
                    offset += written
                    handle.flush()
                    self._write_checkpoint(
                        checkpoint, fingerprint, offset, len(data), data)
        except ClipRangeError:
            raise
        except OSError as exc:
            try:
                actual = os.path.getsize(partial)
            except OSError:
                actual = offset
            try:
                self._write_checkpoint(
                    checkpoint, fingerprint, actual, len(data), data)
            except OSError:
                pass
            raise ClipRangeError(
                "disk full or I/O error while exporting clip: %s" % exc
            ) from exc

        if offset != len(data):
            raise ClipRangeError("disk full while exporting clip")

        self._cleanup_output_state()

    def export(self, audio_priming_samples=0, disk_limit_bytes=None):
        priming = _integer(
            audio_priming_samples,
            "audio_priming_samples",
            minimum=0,
        )
        if disk_limit_bytes is not None:
            disk_limit = _integer(
                disk_limit_bytes, "disk_limit_bytes", minimum=0)
        else:
            disk_limit = None

        self._ensure_range()
        self._last_priming = priming
        self._disk_limit = disk_limit
        meta = self._build_metadata(priming)
        self._decoded_frames = meta["decoded_frames"]
        data = self._build_export_bytes(meta)
        fingerprint = self._fingerprint(meta)

        self._write_resumable(data, fingerprint, disk_limit)
        self._completed_bytes = data
        self._completed_fingerprint = fingerprint
        self._last_meta = meta
        return meta

    def export_bytes(self):
        self._ensure_range()
        meta = self._build_metadata(self._last_priming)
        self._decoded_frames = meta["decoded_frames"]
        return self._build_export_bytes(meta)

    def resume(self):
        self._ensure_range()
        meta = self._build_metadata(self._last_priming)
        data = self._build_export_bytes(meta)
        fingerprint = self._fingerprint(meta)

        partial, _ = self._state_paths(fingerprint)
        if os.path.exists(partial):
            self._decoded_frames = meta["decoded_frames"]
            self._write_resumable(data, fingerprint, None)
            self._completed_bytes = data
            self._completed_fingerprint = fingerprint
            self._last_meta = meta
            return data

        if (self._completed_bytes is not None and
                self._completed_fingerprint == fingerprint):
            return self._completed_bytes

        self.export(audio_priming_samples=self._last_priming)
        return self._completed_bytes

    def subtitles(self, cues):
        self._ensure_range()
        in_time = self._frame_time_ms(self._in_frame)
        out_time = self._frame_time_ms(self._out_frame)
        remaining = []
        try:
            iterator = iter(cues)
        except TypeError as exc:
            raise ClipRangeError("cues must be an iterable") from exc
        for cue in iterator:
            if not isinstance(cue, dict):
                raise ClipRangeError("each subtitle cue must be a mapping")
            if "start_ms" not in cue or "end_ms" not in cue:
                raise ClipRangeError(
                    "subtitle cue needs start_ms and end_ms")
            start = _fraction(cue["start_ms"], "subtitle start_ms")
            end = _fraction(cue["end_ms"], "subtitle end_ms")
            if end < start:
                raise ClipRangeError(
                    "subtitle cue ends before it starts")
            if start >= in_time and end <= out_time:
                remaining.append(dict(cue))
        return remaining
