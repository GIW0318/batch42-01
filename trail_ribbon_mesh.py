import math


class TrailError(Exception):
    pass


_EPSILON = 1e-9


def _distance(a, b):
    return ((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2 + (a[2] - b[2]) ** 2) ** 0.5


def _lerp(a, b, t):
    return a + (b - a) * t


def _lerp_position(a, b, t):
    return (_lerp(a[0], b[0], t), _lerp(a[1], b[1], t), _lerp(a[2], b[2], t))


def _lerp_matrix(a, b, t):
    return tuple(
        tuple(_lerp(a[r][c], b[r][c], t) for c in range(3)) for r in range(3)
    )


class TrailRibbon:
    def __init__(self, max_segments=64, lifetime_ms=300, max_segment_length=10.0,
                 time_base_ms=10.0):
        if max_segments < 0:
            raise TrailError("max_segments must not be negative")
        self.max_segments = max_segments
        self.lifetime_ms = lifetime_ms
        self.max_segment_length = max_segment_length
        self.time_base_ms = time_base_ms
        self.half_width = 0.5
        self._samples = []
        self._paused = False
        self._allocations = 0
        self._vertex_count = 0
        self._vertex_pool = None
        self._color = (1.0, 1.0, 1.0, 1.0)
        self._matrix = ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0))

    def add_sample(self, timestamp_ms, bone_matrix, position):
        if self._paused:
            return False
        if bone_matrix is None:
            raise TrailError("bone matrix is missing")
        timestamp = float(timestamp_ms)
        sample = {
            "timestamp": timestamp,
            "bone_matrix": bone_matrix,
            "position": tuple(position),
        }
        if self._samples:
            last = self._samples[-1]["timestamp"]
            if timestamp < last:
                raise TrailError("timestamp regression")
            if timestamp == last:
                self._samples[-1] = sample
                return True
        self._samples.append(sample)
        return True

    def build(self, now_ms):
        self._ensure_pool()
        cutoff = float(now_ms) - self.lifetime_ms
        self._samples = [s for s in self._samples if s["timestamp"] >= cutoff]
        samples = self._resample(self._samples)
        if self.max_segments == 0:
            samples = []
        elif len(samples) > self.max_segments:
            samples = samples[-self.max_segments:]

        vertices = []
        count = len(samples)
        for index, sample in enumerate(samples):
            perp = self._perpendicular(samples, index)
            v = index / (count - 1) if count > 1 else 0.0
            px, py, pz = sample["position"]
            ox, oy, oz = (perp[0] * self.half_width,
                          perp[1] * self.half_width,
                          perp[2] * self.half_width)
            vertices.append({
                "position": (px - ox, py - oy, pz - oz),
                "uv": (0.0, v),
            })
            vertices.append({
                "position": (px + ox, py + oy, pz + oz),
                "uv": (1.0, v),
            })

        triangles = []
        segment_lengths = []
        for index in range(count - 1):
            length = _distance(samples[index]["position"],
                               samples[index + 1]["position"])
            if length <= _EPSILON or length > self.max_segment_length:
                continue
            segment_lengths.append(length)
            area = 0.5 * (2.0 * self.half_width) * length
            triangles.append(area)
            triangles.append(area)

        self._vertex_count = len(vertices)
        return {
            "samples": samples,
            "vertices": vertices,
            "triangles": triangles,
            "segment_lengths": segment_lengths,
        }

    def set_paused(self, paused):
        paused = bool(paused)
        if self._paused and not paused:
            self._samples = []
        self._paused = paused

    def release(self):
        self._samples = []
        self._vertex_pool = None
        self._vertex_count = 0

    def set_material(self, color):
        self._color = tuple(color)

    def material(self):
        return self._color

    def pool_stats(self):
        return {
            "allocations": self._allocations,
            "vertex_count": self._vertex_count,
            "capacity": len(self._vertex_pool) if self._vertex_pool is not None else 0,
        }

    def sort_indices(self, camera_position):
        segments = []
        for index in range(len(self._samples) - 1):
            a = self._samples[index]["position"]
            b = self._samples[index + 1]["position"]
            length = _distance(a, b)
            if length <= _EPSILON or length > self.max_segment_length:
                continue
            midpoint = ((a[0] + b[0]) * 0.5, (a[1] + b[1]) * 0.5,
                        (a[2] + b[2]) * 0.5)
            distance = _distance(midpoint, camera_position)
            segments.append((distance, index))
        segments.sort(key=lambda item: item[0], reverse=True)
        return [index for _, index in segments]

    def samples(self):
        return [dict(sample) for sample in self._samples]

    def _ensure_pool(self):
        if self._vertex_pool is None:
            self._vertex_pool = [None] * (self.max_segments * 2)
            self._allocations += 1

    def _resample(self, samples):
        if len(samples) < 2 or self.time_base_ms <= 0:
            return list(samples)
        base = self.time_base_ms
        out = [samples[0]]
        for previous, current in zip(samples, samples[1:]):
            t0 = previous["timestamp"]
            t1 = current["timestamp"]
            first = math.floor(t0 / base) + 1
            last = math.ceil(t1 / base) - 1
            step = first
            while step <= last:
                t = step * base
                if t0 < t < t1:
                    alpha = (t - t0) / (t1 - t0)
                    out.append({
                        "timestamp": t,
                        "bone_matrix": _lerp_matrix(
                            previous["bone_matrix"], current["bone_matrix"], alpha),
                        "position": _lerp_position(
                            previous["position"], current["position"], alpha),
                    })
                step += 1
            out.append(current)
        return out

    def _perpendicular(self, samples, index):
        count = len(samples)
        if count < 2:
            return (0.0, 1.0, 0.0)
        if index == 0:
            a, b = samples[0]["position"], samples[1]["position"]
        elif index == count - 1:
            a, b = samples[-2]["position"], samples[-1]["position"]
        else:
            a, b = samples[index - 1]["position"], samples[index + 1]["position"]
        dx, dy, dz = b[0] - a[0], b[1] - a[1], b[2] - a[2]
        norm = (dx * dx + dy * dy + dz * dz) ** 0.5
        if norm <= _EPSILON:
            return (0.0, 1.0, 0.0)
        dx, dy, dz = dx / norm, dy / norm, dz / norm
        px, py = dy, -dx
        pnorm = (px * px + py * py) ** 0.5
        if pnorm <= _EPSILON:
            return (0.0, 1.0, 0.0)
        return (px / pnorm, py / pnorm, 0.0)
