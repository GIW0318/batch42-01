class StreamSourceError(Exception):
    pass


VALID_TOKEN = "ok"


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
        self._switch_count = 0
        self._switched_at_ms = None
        self._restored = False

    @staticmethod
    def _token_valid(source):
        return source["token"] == VALID_TOKEN

    def _health_of(self, source_id):
        return self._health.setdefault(source_id, {
            "consecutive_failures": 0,
            "healthy_since": None,
            "ever_failed": False,
            "last_report_ms": None,
        })

    def _is_healthy(self, source_id):
        health = self._health.get(source_id)
        if health is None or not health["ever_failed"]:
            return True
        return health["healthy_since"] is not None

    def add_source(self, source_id, url, priority, token="ok",
                   resolution=(1920, 1080), bitrate=4000):
        if not url:
            raise StreamSourceError("source url must not be empty")
        self._sources[source_id] = {
            "source_id": source_id,
            "url": url,
            "priority": priority,
            "token": token,
            "resolution": tuple(resolution),
            "bitrate": bitrate,
        }

    def report_health(self, source_id, ok, now_ms):
        if source_id not in self._sources:
            raise StreamSourceError("unknown source: %s" % source_id)
        health = self._health_of(source_id)
        if ok:
            if health["consecutive_failures"] > 0 or (
                    health["ever_failed"] and health["healthy_since"] is None):
                health["healthy_since"] = now_ms
            health["consecutive_failures"] = 0
        else:
            health["consecutive_failures"] += 1
            health["ever_failed"] = True
            health["healthy_since"] = None
        health["last_report_ms"] = now_ms

    def _flap_limited(self, now_ms):
        floor = now_ms - self.flap_window_ms
        self._switches = [t for t in self._switches if t >= floor]
        return len(self._switches) >= self.max_switches_per_window

    def trigger_failover(self, now_ms):
        if self._flap_limited(now_ms):
            return None
        candidates = sorted(
            (s for s in self._sources.values()
             if s["source_id"] != self._current),
            key=lambda s: s["priority"],
        )
        for source in candidates:
            source_id = source["source_id"]
            if not self._token_valid(source):
                continue
            if not self._is_healthy(source_id):
                continue
            self._pending = source_id
            self._switches.append(now_ms)
            return source_id
        return None

    def request_switch(self, request_id, source_id, now_ms):
        if request_id in self._requests:
            return False
        if source_id not in self._sources:
            raise StreamSourceError("unknown source: %s" % source_id)
        source = self._sources[source_id]
        if not self._token_valid(source):
            raise StreamSourceError(
                "auth failed for source: %s" % source_id)
        if source_id == self._current:
            return False
        health = self._health.get(source_id)
        if health is not None and health["ever_failed"]:
            healthy_since = health["healthy_since"]
            if healthy_since is None or \
                    now_ms - healthy_since < self.recovery_ms:
                return False
        self._requests.add(request_id)
        self._pending = source_id
        return True

    def ingest_segment(self, source_id, seq, keyframe, timestamp_ms,
                       audio_ms=None):
        if source_id not in self._sources:
            raise StreamSourceError("unknown source: %s" % source_id)
        if self._current is not None and source_id != self._current \
                and source_id != self._pending:
            return None
        if seq <= self._last_seq:
            if self._restored:
                return None
            raise StreamSourceError("duplicate segment sequence: %s" % seq)
        starting_source = self._current is None or source_id == self._pending
        if starting_source and not keyframe:
            raise StreamSourceError(
                "new source must start with a keyframe")
        if self._last_timestamp is not None \
                and timestamp_ms <= self._last_timestamp:
            raise StreamSourceError(
                "timestamp must move forward across the output")
        source = self._sources[source_id]
        if starting_source:
            if self._current is not None:
                self._switch_count += 1
            self._current = source_id
            self._pending = None
            self._switched_at_ms = timestamp_ms
            self._recordings.append({
                "source_id": source_id,
                "header": True,
                "started_at_ms": timestamp_ms,
                "resolution": source["resolution"],
                "bitrate": source["bitrate"],
            })
        segment = {
            "seq": seq,
            "source_id": source_id,
            "keyframe": keyframe,
            "timestamp_ms": timestamp_ms,
            "audio_ms": timestamp_ms,
            "resolution": source["resolution"],
            "bitrate": source["bitrate"],
        }
        self._output.append(segment)
        self._last_seq = seq
        self._last_timestamp = timestamp_ms
        self._restored = False
        return segment

    def current(self):
        return {
            "source_id": self._current,
            "pending": self._pending,
            "switched_at_ms": self._switched_at_ms,
            "switch_count": self._switch_count,
        }

    def output(self):
        return list(self._output)

    def recording_header(self):
        if not self._recordings:
            return None
        return dict(self._recordings[-1])

    def snapshot(self):
        def pack_segment(segment):
            packed = dict(segment)
            packed["resolution"] = list(packed["resolution"])
            return packed

        return {
            "config": {
                "health_threshold": self.health_threshold,
                "recovery_ms": self.recovery_ms,
                "flap_window_ms": self.flap_window_ms,
                "max_switches_per_window": self.max_switches_per_window,
            },
            "sources": {
                sid: dict(source, resolution=list(source["resolution"]))
                for sid, source in self._sources.items()
            },
            "health": {sid: dict(h) for sid, h in self._health.items()},
            "current": self._current,
            "pending": self._pending,
            "output": [pack_segment(s) for s in self._output],
            "requests": sorted(self._requests),
            "switches": list(self._switches),
            "last_seq": self._last_seq,
            "last_timestamp": self._last_timestamp,
            "recordings": [
                dict(r, resolution=list(r["resolution"]))
                for r in self._recordings
            ],
            "switch_count": self._switch_count,
            "switched_at_ms": self._switched_at_ms,
        }

    def restore(self, snapshot):
        config = snapshot.get("config", {})
        self.health_threshold = config.get(
            "health_threshold", self.health_threshold)
        self.recovery_ms = config.get("recovery_ms", self.recovery_ms)
        self.flap_window_ms = config.get(
            "flap_window_ms", self.flap_window_ms)
        self.max_switches_per_window = config.get(
            "max_switches_per_window", self.max_switches_per_window)
        self._sources = {
            sid: dict(source, resolution=tuple(source["resolution"]))
            for sid, source in snapshot["sources"].items()
        }
        self._health = {
            sid: dict(h) for sid, h in snapshot["health"].items()
        }
        self._current = snapshot["current"]
        self._pending = snapshot["pending"]
        self._output = [
            dict(s, resolution=tuple(s["resolution"]))
            for s in snapshot["output"]
        ]
        self._requests = set(snapshot["requests"])
        self._switches = list(snapshot["switches"])
        self._last_seq = snapshot["last_seq"]
        self._last_timestamp = snapshot["last_timestamp"]
        self._recordings = [
            dict(r, resolution=tuple(r["resolution"]))
            for r in snapshot["recordings"]
        ]
        self._switch_count = snapshot["switch_count"]
        self._switched_at_ms = snapshot["switched_at_ms"]
        self._restored = True
