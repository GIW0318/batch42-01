"""Screen capture frame/audio timestamping and file finalization (skeleton).

Audio and video are aligned against the first packet, rotation and resolution
changes start new tracks, pause keeps the timeline continuous and a failed
recording can recover a playable file from parts.  No behaviour implemented.
"""

ENCODER_FAILED = "encoder_failed"
NON_MONOTONIC = "non_monotonic"
NO_SOURCE = "no_source"


class CaptureError(Exception):
    def __init__(self, code, message=""):
        self.code = code
        super().__init__(code if not message else "%s: %s" % (code, message))


class ScreenCapturePipeline:
    def __init__(self, timebase=90000, drift_limit=0.05):
        self.timebase = timebase
        self.drift_limit = drift_limit

    def start(self, config=None):
        raise NotImplementedError

    def add_video_frame(self, pts, duration=1, rotation=0, width=1920,
                        height=1080, keyframe=False):
        raise NotImplementedError

    def add_audio_packet(self, pts, duration=1, channels=2):
        raise NotImplementedError

    def pause(self):
        raise NotImplementedError

    def resume(self):
        raise NotImplementedError

    def finalize(self):
        raise NotImplementedError

    def metadata(self):
        raise NotImplementedError

    def recover_parts(self):
        raise NotImplementedError

    def set_source(self, source_id):
        raise NotImplementedError

    def add_dropped_frame(self, count=1):
        raise NotImplementedError

    def fail_encoder(self):
        raise NotImplementedError

    def set_cursor(self, enabled):
        raise NotImplementedError

    def set_hdr(self, enabled):
        raise NotImplementedError
