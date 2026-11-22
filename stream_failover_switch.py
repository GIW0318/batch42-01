class StreamSourceError(Exception):
    pass


class FailoverSwitch:
    def __init__(self, health_threshold=3, recovery_ms=5000,
                 flap_window_ms=60000, max_switches_per_window=1):
        self.health_threshold = health_threshold
        self.recovery_ms = recovery_ms
        self.flap_window_ms = flap_window_ms
        self.max_switches_per_window = max_switches_per_window
        self._sources = {}
        self._health = {}
        self._current = None
        self._pending = None
        self._output = []
        self._requests = set()
        self._switches = []
        self._last_seq = 0
        self._last_timestamp = None
        self._recordings = []

    def add_source(self, source_id, url, priority, token="ok",
                   resolution=(1920, 1080), bitrate=4000):
        raise NotImplementedError()

    def report_health(self, source_id, ok, now_ms):
        raise NotImplementedError()

    def trigger_failover(self, now_ms):
        raise NotImplementedError()

    def request_switch(self, request_id, source_id, now_ms):
        raise NotImplementedError()

    def ingest_segment(self, source_id, seq, keyframe, timestamp_ms,
                       audio_ms=None):
        raise NotImplementedError()

    def current(self):
        raise NotImplementedError()

    def output(self):
        raise NotImplementedError()

    def recording_header(self):
        raise NotImplementedError()

    def snapshot(self):
        raise NotImplementedError()

    def restore(self, snapshot):
        raise NotImplementedError()
