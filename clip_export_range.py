class ClipRangeError(Exception):
    pass


class ClipRange:
    def __init__(self, duration_ms, fps, keyframes, rotation=0, vfr=False,
                 sample_rate=48000):
        self.duration_ms = duration_ms
        self.fps = fps
        self.keyframes = list(keyframes)
        self.rotation = rotation
        self.vfr = vfr
        self.sample_rate = sample_rate
        self._in_frame = 0
        self._out_frame = 0
        self._disk_limit = None

    def frame_at_ms(self, ms):
        raise NotImplementedError()

    def set_range(self, in_frame, out_frame):
        raise NotImplementedError()

    def export(self, audio_priming_samples=0, disk_limit_bytes=None):
        raise NotImplementedError()

    def export_bytes(self):
        raise NotImplementedError()

    def resume(self):
        raise NotImplementedError()

    def metadata(self):
        raise NotImplementedError()

    def subtitles(self, cues):
        raise NotImplementedError()

    def decoded_frames(self):
        raise NotImplementedError()
