class TunerDeviceError(Exception):
    pass


class PitchTuner:
    def __init__(self, sample_rate=44100, window_ms=80.0, a4=440.0,
                 confidence_threshold=0.8):
        self.sample_rate = sample_rate
        self.window_ms = window_ms
        self.a4 = a4
        self.confidence_threshold = confidence_threshold
        self._buffer = []
        self._result = {"detected": False}
        self._mode = "single"
        self._analyzed_frames = 0

    def open_device(self, sample_rate):
        raise NotImplementedError()

    def push_samples(self, samples):
        raise NotImplementedError()

    def analyze(self):
        raise NotImplementedError()

    def set_a4(self, hz):
        raise NotImplementedError()

    def set_mode(self, mode="single"):
        raise NotImplementedError()

    def window_samples(self):
        raise NotImplementedError()

    def analyzed_frames(self):
        raise NotImplementedError()

    def reset(self):
        raise NotImplementedError()
