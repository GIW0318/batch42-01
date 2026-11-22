import bisect
import math
from dataclasses import dataclass, field


class HapticConfigError(Exception):
    pass


_EPS = 1e-9


@dataclass
class _Effect:
    effect_id: object
    duration_ms: float
    channels: int
    priority: float
    curve: tuple


@dataclass
class _Request:
    request_id: object
    effect: _Effect
    intensity: float
    start_ms: float
    elapsed_ms: float = 0.0
    started: bool = False
    finished: bool = False


@dataclass
class _Device:
    sample_rate: float
    channel_map: tuple
    clock_ms: float
    next_sample_ms: float
    heat: float = 1.0
    stopped: bool = False
    last_sample_ms: float = None
    requests: list = field(default_factory=list)
    samples: list = field(default_factory=list)


class HapticTimeline:
    def __init__(self, sample_rate=1000, max_intensity=1.0):
        if not _is_number(sample_rate) or sample_rate <= 0:
            raise HapticConfigError("sample_rate must be positive")
        if not _is_number(max_intensity) or max_intensity <= 0:
            raise HapticConfigError("max_intensity must be positive")

        self.sample_rate = float(sample_rate)
        self.max_intensity = float(max_intensity)
        self._effects = {}
        self._devices = {}
        self._played = set()
        self._samples = {}
        self._paused = False
        self._interval_ms = 1000.0 / self.sample_rate
        self._frame_budget_ms = 0.0
        self._closed = False
        self._wall_clock_ms = 0.0
        self._timeline_ms = 0.0
        self._paused_total_ms = 0.0
        self._pause_started_ms = 0.0

    def define_effect(self, effect_id, duration_ms, channels=1, priority=0,
                      curve=None):
        if self._closed:
            raise HapticConfigError("timeline is closed")
        if not _is_hashable(effect_id):
            raise HapticConfigError("effect_id must be hashable")
        if not _is_number(duration_ms) or duration_ms < 0:
            raise HapticConfigError("duration_ms must be non-negative")
        if isinstance(channels, bool) or not isinstance(channels, int):
            raise HapticConfigError("channels must be a positive integer")
        if channels <= 0:
            raise HapticConfigError("channels must be a positive integer")
        if not _is_number(priority):
            raise HapticConfigError("priority must be a number")

        effect = _Effect(
            effect_id=effect_id,
            duration_ms=float(duration_ms),
            channels=channels,
            priority=float(priority),
            curve=self._normalise_curve(duration_ms, channels, curve),
        )
        self._effects[effect_id] = effect
        return effect_id

    def play(self, request_id, effect_id, device_id, at_ms=0.0,
             intensity=1.0):
        if self._closed:
            raise HapticConfigError("timeline is closed")
        if not _is_hashable(request_id) or not _is_hashable(device_id):
            raise HapticConfigError("request_id and device_id must be hashable")

        request_key = (device_id, request_id)
        if request_key in self._played:
            return False

        effect = self._effects.get(effect_id)
        if effect is None:
            raise HapticConfigError("unknown effect: %r" % (effect_id,))
        device = self._devices.get(device_id)
        if device is None:
            raise HapticConfigError("unknown device: %r" % (device_id,))
        if not _is_number(intensity):
            raise HapticConfigError("intensity must be a number")
        if intensity < 0 or intensity > self.max_intensity:
            raise HapticConfigError("intensity is outside the valid range")
        if not _is_number(at_ms) or at_ms < 0:
            raise HapticConfigError("at_ms must be non-negative")

        self._validate_channel_layout(effect, device)

        start_ms = max(float(at_ms), self._timeline_now())
        device.clock_ms = max(device.clock_ms, self._timeline_now())
        if device.next_sample_ms < self._wall_clock_ms - _EPS:
            device.next_sample_ms = self._wall_clock_ms

        device.requests.append(_Request(
            request_id=request_id,
            effect=effect,
            intensity=float(intensity),
            start_ms=start_ms,
        ))
        device.stopped = False
        self._played.add(request_key)
        return True

    def stop(self, device_id):
        device = self._devices.get(device_id)
        if device is None:
            return False
        self._emit_stop(device)
        return True

    def advance(self, now_ms):
        if self._closed:
            return
        if not _is_number(now_ms):
            raise HapticConfigError("now_ms must be a number")

        target_ms = max(float(now_ms), self._wall_clock_ms)
        if not self._paused:
            timeline_target = target_ms - self._paused_total_ms
            for device in self._devices.values():
                if device.stopped or not device.requests:
                    continue
                period = self._sample_period(device)
                while device.next_sample_ms <= target_ms + _EPS:
                    sample_at = device.next_sample_ms
                    timeline_at = sample_at - self._paused_total_ms
                    if timeline_at < device.clock_ms:
                        timeline_at = device.clock_ms
                    self._simulate(device, timeline_at)
                    device.samples.append({
                        "at_ms": float(sample_at),
                        "channels": self._render(device, timeline_at),
                    })
                    device.last_sample_ms = float(sample_at)
                    device.next_sample_ms = sample_at + period
                self._simulate(device, timeline_target)
            self._timeline_ms = timeline_target
        else:
            for device in self._devices.values():
                if device.stopped or not device.requests:
                    continue
                period = self._sample_period(device)
                zeros = [0.0] * len(device.channel_map)
                while device.next_sample_ms <= target_ms + _EPS:
                    sample_at = device.next_sample_ms
                    device.samples.append({
                        "at_ms": float(sample_at),
                        "channels": list(zeros),
                    })
                    device.last_sample_ms = float(sample_at)
                    device.next_sample_ms = sample_at + period

        self._wall_clock_ms = target_ms

    def set_device(self, device_id, sample_rate=1000, channel_map=(0, 1)):
        if self._closed:
            raise HapticConfigError("timeline is closed")
        if not _is_hashable(device_id):
            raise HapticConfigError("device_id must be hashable")
        if not _is_number(sample_rate) or sample_rate <= 0:
            raise HapticConfigError("sample_rate must be positive")
        if channel_map is None:
            raise HapticConfigError("channel_map must not be empty")
        try:
            channel_map = tuple(channel_map)
        except TypeError:
            raise HapticConfigError("channel_map must be a sequence")
        if not channel_map:
            raise HapticConfigError("channel_map must not be empty")
        for channel in channel_map:
            if (isinstance(channel, bool) or not isinstance(channel, int)
                    or channel < 0):
                raise HapticConfigError(
                    "channel_map entries must be non-negative integers")

        old_device = self._devices.get(device_id)
        if old_device is not None:
            self._emit_stop(old_device)

        now = self._timeline_now()
        device = _Device(
            sample_rate=float(sample_rate),
            channel_map=channel_map,
            clock_ms=now,
            next_sample_ms=self._wall_clock_ms,
        )
        self._devices[device_id] = device
        self._samples[device_id] = device.samples

    def remove_device(self, device_id):
        device = self._devices.pop(device_id, None)
        self._samples.pop(device_id, None)
        if device is None:
            return False
        device.requests.clear()
        device.samples.clear()
        device.stopped = True
        return True

    def set_paused(self, paused):
        paused = bool(paused)
        if paused == self._paused:
            return
        if paused:
            self._simulate_all(self._timeline_now())
            self._pause_started_ms = self._wall_clock_ms
            self._paused = True
            for device in self._devices.values():
                if not device.stopped and device.requests:
                    self._append_zero_sample(device, self._wall_clock_ms)
        else:
            self._paused_total_ms += (
                self._wall_clock_ms - self._pause_started_ms)
            self._paused = False

    def report_heat(self, device_id, factor):
        device = self._devices.get(device_id)
        if device is None:
            raise HapticConfigError("unknown device: %r" % (device_id,))
        if not _is_number(factor) or factor < 0 or factor > 1:
            raise HapticConfigError("heat factor must be between 0 and 1")
        device.heat = float(factor)

    def frame_budget(self, ms):
        if not _is_number(ms) or ms < 0:
            raise HapticConfigError("frame budget must be non-negative")
        self._frame_budget_ms = float(ms)
        for device in self._devices.values():
            if device.last_sample_ms is not None:
                earliest = device.last_sample_ms + self._sample_period(device)
                device.next_sample_ms = max(device.next_sample_ms, earliest)

    def output(self, device_id):
        device = self._devices.get(device_id)
        if device is None:
            return []
        samples = device.samples
        device.samples = []
        self._samples[device_id] = device.samples
        return samples

    def close(self):
        if self._closed:
            return {device_id: True for device_id in self._devices}
        stopped = {}
        for device_id, device in self._devices.items():
            self._emit_stop(device)
            stopped[device_id] = True
        self._closed = True
        return stopped

    def _normalise_curve(self, duration_ms, channels, curve):
        duration_ms = float(duration_ms)
        if curve is None:
            point = (0.0,) + (1.0,) * channels
            if duration_ms <= 0:
                return (point,)
            end = (duration_ms,) + (1.0,) * channels
            return (point, end)

        try:
            raw_points = list(curve)
        except TypeError:
            raise HapticConfigError("curve must be a sequence of points")
        if not raw_points:
            raise HapticConfigError("curve must not be empty")

        points = []
        for raw_point in raw_points:
            try:
                point = tuple(raw_point)
            except TypeError:
                raise HapticConfigError("curve points must be sequences")
            if len(point) != channels + 1:
                raise HapticConfigError(
                    "curve channel count does not match the effect")

            timestamp = point[0]
            if not _is_number(timestamp):
                raise HapticConfigError("curve time must be a number")
            timestamp = float(timestamp)
            if timestamp < 0:
                raise HapticConfigError("curve time must be non-negative")

            values = []
            for value in point[1:]:
                if not _is_number(value):
                    raise HapticConfigError("curve intensity must be a number")
                value = float(value)
                if value < 0 or value > self.max_intensity + _EPS:
                    raise HapticConfigError(
                        "curve intensity is outside the valid range")
                values.append(min(value, self.max_intensity))

            current = (timestamp,) + tuple(values)
            if points:
                previous = points[-1]
                delta_t = current[0] - previous[0]
                if delta_t < -_EPS:
                    raise HapticConfigError(
                        "curve times must be non-decreasing")
                if abs(delta_t) <= _EPS:
                    if any(abs(a - b) > _EPS
                           for a, b in zip(current[1:], previous[1:])):
                        raise HapticConfigError(
                            "curve contains a discontinuous jump")
                    continue
                jump = max(abs(a - b)
                           for a, b in zip(current[1:], previous[1:]))
                if delta_t <= 1.0 + _EPS and (
                        jump > 0.5 * self.max_intensity + _EPS):
                    raise HapticConfigError(
                        "curve contains a discontinuous jump")
            points.append(current)

        if not points:
            raise HapticConfigError("curve must not be empty")
        return tuple(points)

    def _validate_channel_layout(self, effect, device):
        output_channels = len(device.channel_map)
        if effect.channels == 1:
            return
        if effect.channels != output_channels:
            raise HapticConfigError(
                "effect and device channel counts do not match")
        if any(channel >= effect.channels for channel in device.channel_map):
            raise HapticConfigError(
                "device channel map references a missing effect channel")

    def _sample_period(self, device):
        native_period = 1000.0 / device.sample_rate
        timeline_period = 1000.0 / self.sample_rate
        return max(native_period, timeline_period, self._frame_budget_ms)

    def _timeline_now(self):
        if self._paused:
            return self._pause_started_ms - self._paused_total_ms
        return self._wall_clock_ms - self._paused_total_ms

    def _simulate_all(self, target_ms):
        for device in self._devices.values():
            if not device.stopped:
                self._simulate(device, target_ms)

    def _simulate(self, device, target_ms):
        if target_ms <= device.clock_ms + _EPS:
            return

        cursor = device.clock_ms
        requests = device.requests
        while cursor < target_ms - _EPS:
            for request in requests:
                if (not request.started
                        and request.start_ms <= cursor + _EPS):
                    request.started = True
                if (request.started
                        and request.effect.duration_ms <= _EPS):
                    request.finished = True
                elif (request.started
                      and request.elapsed_ms >=
                      request.effect.duration_ms - _EPS):
                    request.finished = True

            active = [request for request in requests
                      if request.started and not request.finished]
            next_event = target_ms
            for request in requests:
                if (not request.started
                        and request.start_ms > cursor + _EPS):
                    next_event = min(next_event, request.start_ms)

            top_requests = []
            if active:
                top_priority = max(
                    request.effect.priority for request in active)
                top_requests = [
                    request for request in active
                    if request.effect.priority == top_priority
                ]
                remaining = min(
                    request.effect.duration_ms - request.elapsed_ms
                    for request in top_requests)
                next_event = min(next_event, cursor + max(0.0, remaining))

            if next_event <= cursor + _EPS:
                next_event = target_ms
            delta = next_event - cursor
            for request in top_requests:
                request.elapsed_ms = min(
                    request.effect.duration_ms,
                    request.elapsed_ms + delta)
                if (request.elapsed_ms >=
                        request.effect.duration_ms - _EPS):
                    request.finished = True
            cursor = next_event

        device.clock_ms = target_ms

    def _render(self, device, at_ms):
        active = []
        for request in device.requests:
            if request.finished or request.start_ms > at_ms + _EPS:
                continue
            if (request.effect.duration_ms <= _EPS
                    or request.elapsed_ms >=
                    request.effect.duration_ms - _EPS):
                continue
            active.append(request)

        channels = [0.0] * len(device.channel_map)
        if active:
            top_priority = max(
                request.effect.priority for request in active)
            for request in active:
                if request.effect.priority != top_priority:
                    continue
                values = self._curve_values(request.effect, request.elapsed_ms)
                values = [value * request.intensity for value in values]
                if request.effect.channels == 1:
                    for index in range(len(channels)):
                        channels[index] += values[0]
                else:
                    for index, source in enumerate(device.channel_map):
                        channels[index] += values[source]

        peak = max(channels) if channels else 0.0
        if peak > self.max_intensity:
            scale = self.max_intensity / peak
            channels = [value * scale for value in channels]

        channels = [
            min(self.max_intensity,
                max(0.0, value * device.heat))
            for value in channels
        ]
        return channels

    def _curve_values(self, effect, elapsed_ms):
        curve = effect.curve
        if len(curve) == 1 or elapsed_ms <= curve[0][0] + _EPS:
            return list(curve[0][1:])
        if elapsed_ms >= curve[-1][0] - _EPS:
            return list(curve[-1][1:])

        times = [point[0] for point in curve]
        index = bisect.bisect_right(times, elapsed_ms) - 1
        left = curve[index]
        right = curve[index + 1]
        span = right[0] - left[0]
        if span <= _EPS:
            return list(right[1:])
        fraction = (elapsed_ms - left[0]) / span
        return [
            start + (end - start) * fraction
            for start, end in zip(left[1:], right[1:])
        ]

    def _append_zero_sample(self, device, at_ms):
        device.samples.append({
            "at_ms": float(at_ms),
            "channels": [0.0] * len(device.channel_map),
        })
        device.last_sample_ms = float(at_ms)

    def _emit_stop(self, device):
        device.requests.clear()
        device.samples.clear()
        device.stopped = True
        self._append_zero_sample(device, self._wall_clock_ms)


def _is_number(value):
    return (not isinstance(value, bool)
            and isinstance(value, (int, float))
            and math.isfinite(value))


def _is_hashable(value):
    try:
        hash(value)
    except TypeError:
        return False
    return True
