class HapticConfigError(Exception):
    pass


class HapticTimeline:
    def __init__(self, sample_rate=1000, max_intensity=1.0):
        self.sample_rate = sample_rate
        self.max_intensity = max_intensity
        self._effects = {}
        self._devices = {}
        self._played = set()
        self._samples = {}
        self._paused = False
        self._interval_ms = 1000.0 / sample_rate
        self._closed = False

    def define_effect(self, effect_id, duration_ms, channels=1, priority=0,
                      curve=None):
        raise NotImplementedError()

    def play(self, request_id, effect_id, device_id, at_ms=0.0,
             intensity=1.0):
        raise NotImplementedError()

    def stop(self, device_id):
        raise NotImplementedError()

    def advance(self, now_ms):
        raise NotImplementedError()

    def set_device(self, device_id, sample_rate=1000, channel_map=(0, 1)):
        raise NotImplementedError()

    def remove_device(self, device_id):
        raise NotImplementedError()

    def set_paused(self, paused):
        raise NotImplementedError()

    def report_heat(self, device_id, factor):
        raise NotImplementedError()

    def frame_budget(self, ms):
        raise NotImplementedError()

    def output(self, device_id):
        raise NotImplementedError()

    def close(self):
        raise NotImplementedError()
