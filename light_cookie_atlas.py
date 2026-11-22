import math
import threading
import time


class CookieAtlasError(Exception):
    pass


_LIGHT_KINDS = ("spot", "point")


def _is_power_of_two(value):
    return isinstance(value, int) and value >= 1 and (value & (value - 1)) == 0


def _srgb_channel_to_linear(channel):
    if channel <= 0.04045:
        return channel / 12.92
    return ((channel + 0.055) / 1.055) ** 2.4


def _srgb_pixel_to_linear(pixel):
    return tuple(_srgb_channel_to_linear(float(c)) for c in pixel)


def _normalize3(v):
    length = math.sqrt(v[0] * v[0] + v[1] * v[1] + v[2] * v[2])
    if length == 0.0:
        raise CookieAtlasError("direction must be a non-zero vector")
    return (v[0] / length, v[1] / length, v[2] / length)


def _cross3(a, b):
    return (a[1] * b[2] - a[2] * b[1],
            a[2] * b[0] - a[0] * b[2],
            a[0] * b[1] - a[1] * b[0])


def _dot3(a, b):
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _mat_mul(a, b):
    return tuple(
        tuple(sum(a[r][k] * b[k][c] for k in range(4)) for c in range(4))
        for r in range(4)
    )


def _view_matrix(position, direction):
    forward = _normalize3(direction)
    up_hint = (0.0, 1.0, 0.0)
    if abs(_dot3(forward, up_hint)) > 0.999:
        up_hint = (0.0, 0.0, 1.0)
    side = _normalize3(_cross3(forward, up_hint))
    up = _cross3(side, forward)
    eye = (float(position[0]), float(position[1]), float(position[2]))
    return (
        (side[0], side[1], side[2], -_dot3(side, eye)),
        (up[0], up[1], up[2], -_dot3(up, eye)),
        (-forward[0], -forward[1], -forward[2], _dot3(forward, eye)),
        (0.0, 0.0, 0.0, 1.0),
    )


def _perspective_matrix(fov_degrees, aspect, near, far):
    f = 1.0 / math.tan(math.radians(fov_degrees) * 0.5)
    depth = near - far
    return (
        (f / aspect, 0.0, 0.0, 0.0),
        (0.0, f, 0.0, 0.0),
        (0.0, 0.0, (far + near) / depth, 2.0 * far * near / depth),
        (0.0, 0.0, -1.0, 0.0),
    )


def _downsample(level):
    size = len(level)
    half = size // 2
    result = []
    for y in range(half):
        row = []
        for x in range(half):
            a = level[2 * y][2 * x]
            b = level[2 * y][2 * x + 1]
            c = level[2 * y + 1][2 * x]
            d = level[2 * y + 1][2 * x + 1]
            row.append(tuple((a[i] + b[i] + c[i] + d[i]) * 0.25
                             for i in range(len(a))))
        result.append(row)
    return result


class _Cookie:
    def __init__(self, colors, spot_only):
        if not colors or not isinstance(colors, (list, tuple)):
            raise CookieAtlasError("cookie colors must be a non-empty grid")
        size = len(colors)
        pixels = []
        for row in colors:
            if not isinstance(row, (list, tuple)) or len(row) != size:
                raise CookieAtlasError("cookie colors must be a square grid")
            pixels.append([_srgb_pixel_to_linear(pixel) for pixel in row])
        self.size = size
        self.spot_only = spot_only
        self.pixels = pixels
        self.mips = self._build_mips()

    def _build_mips(self):
        mips = [self.pixels]
        level = self.pixels
        while len(level) > 1:
            level = _downsample(level)
            mips.append(level)
        return mips

    def sample(self, u, v):
        u = min(max(float(u), 0.0), 1.0)
        v = min(max(float(v), 0.0), 1.0)
        size = self.size
        if size == 1:
            return self.pixels[0][0]
        x = u * (size - 1)
        y = v * (size - 1)
        x0 = int(math.floor(x))
        y0 = int(math.floor(y))
        x1 = min(x0 + 1, size - 1)
        y1 = min(y0 + 1, size - 1)
        fx = x - x0
        fy = y - y0
        top = self._lerp(self.pixels[y0][x0], self.pixels[y0][x1], fx)
        bottom = self._lerp(self.pixels[y1][x0], self.pixels[y1][x1], fx)
        return self._lerp(top, bottom, fy)

    @staticmethod
    def _lerp(a, b, t):
        return tuple(a[i] + (b[i] - a[i]) * t for i in range(len(a)))


class _LightEntry:
    __slots__ = ("light_id", "cookie_id", "slot", "size", "kind")

    def __init__(self, light_id, cookie_id, slot, size, kind):
        self.light_id = light_id
        self.cookie_id = cookie_id
        self.slot = slot
        self.size = size
        self.kind = kind


class CookieAtlas:
    def __init__(self, atlas_size=1024, cell_size=256, max_lights=4):
        self.atlas_size = atlas_size
        self.cell_size = cell_size
        self.max_lights = max_lights
        cells_per_row = max(1, atlas_size // max(1, cell_size))
        self._capacity = min(max_lights, cells_per_row * cells_per_row)
        self._lights = {}
        self._slots = {}
        self._cookies = {}
        self._animations = {}
        self._budget_ms = None
        self._lock = threading.RLock()

    def register_light(self, light_id, cookie_id, size=256, kind="spot"):
        with self._lock:
            if not isinstance(light_id, str) or not light_id:
                raise CookieAtlasError("invalid light id: %r" % (light_id,))
            existing = self._lights.get(light_id)
            if existing is not None:
                return existing.slot
            if kind not in _LIGHT_KINDS:
                raise CookieAtlasError("unsupported light kind: %r" % (kind,))
            cookie = self._cookies.get(cookie_id)
            if cookie is None:
                raise CookieAtlasError("unknown cookie: %r" % (cookie_id,))
            self._validate_fit(cookie, size, kind)
            slot = self._allocate_slot()
            self._lights[light_id] = _LightEntry(light_id, cookie_id, slot,
                                                 size, kind)
            self._slots[slot] = light_id
            return slot

    def _validate_fit(self, cookie, size, kind):
        if cookie.spot_only and kind == "point":
            raise CookieAtlasError(
                "spot-only cookie cannot be used by a point light")
        if not _is_power_of_two(size) or size < 2:
            raise CookieAtlasError("slot size must be a power of two >= 2")
        if not _is_power_of_two(cookie.size) or cookie.size < 2:
            raise CookieAtlasError(
                "cookie size %d does not fit a %d slot"
                % (cookie.size, size))
        if cookie.size > size:
            raise CookieAtlasError(
                "cookie size %d exceeds slot size %d"
                % (cookie.size, size))

    def _allocate_slot(self):
        for slot in range(self._capacity):
            if slot not in self._slots:
                return slot
        raise CookieAtlasError("cookie atlas slots exhausted")

    def release_light(self, light_id):
        with self._lock:
            entry = self._lights.pop(light_id, None)
            if entry is None:
                raise CookieAtlasError("unknown light id: %r" % (light_id,))
            del self._slots[entry.slot]

    def slot_of(self, light_id):
        entry = self._lights.get(light_id)
        return entry.slot if entry is not None else None

    def projection_matrix(self, light_id, position, direction, fov, near=0.1,
                          far=100.0, aspect=1.0):
        if light_id not in self._lights:
            raise CookieAtlasError("unknown light id: %r" % (light_id,))
        if not (0.0 < fov < 180.0):
            raise CookieAtlasError("fov out of range: %r" % (fov,))
        if aspect <= 0.0:
            raise CookieAtlasError("aspect must be positive")
        if near <= 0.0 or far <= near:
            raise CookieAtlasError("invalid near/far clip planes")
        view = _view_matrix(position, direction)
        projection = _perspective_matrix(fov, aspect, near, far)
        return _mat_mul(projection, view)

    def set_cookie(self, cookie_id, colors, spot_only=False):
        with self._lock:
            self._cookies[cookie_id] = _Cookie(colors, spot_only)

    def sample(self, light_id, uv):
        entry = self._lights.get(light_id)
        if entry is None:
            raise CookieAtlasError("unknown light id: %r" % (light_id,))
        cookie = self._cookies.get(entry.cookie_id)
        if cookie is None:
            raise CookieAtlasError("unknown cookie: %r" % (entry.cookie_id,))
        return cookie.sample(uv[0], uv[1])

    def set_animation(self, cookie_id, frames, fps):
        if not isinstance(frames, int) or frames < 1:
            raise CookieAtlasError("animation needs at least one frame")
        if fps <= 0:
            raise CookieAtlasError("animation fps must be positive")
        self._animations[cookie_id] = (frames, float(fps))

    def frame_at(self, cookie_id, time_ms):
        animation = self._animations.get(cookie_id)
        if animation is None:
            raise CookieAtlasError("no animation for cookie: %r"
                                   % (cookie_id,))
        frames, fps = animation
        frame = int(math.floor(float(time_ms) * fps / 1000.0))
        if frame < 0 or frame >= frames:
            raise CookieAtlasError(
                "animation frame %d out of range [0, %d)" % (frame, frames))
        return frame

    def set_budget(self, ms):
        if ms is not None and ms < 0:
            raise CookieAtlasError("budget must be non-negative")
        self._budget_ms = ms

    def update(self, lights):
        start = time.perf_counter()
        processed = 0
        deferred = False
        for request in lights:
            if self._over_budget(start):
                deferred = True
                break
            self._apply_update(request)
            processed += 1
            if self._over_budget(start):
                deferred = True
                break
        return {"processed": processed, "deferred": deferred}

    def _over_budget(self, start):
        if self._budget_ms is None:
            return False
        elapsed_ms = (time.perf_counter() - start) * 1000.0
        return elapsed_ms >= self._budget_ms

    def _apply_update(self, request):
        light_id = request.get("light_id")
        cookie_id = request.get("cookie_id")
        with self._lock:
            entry = self._lights.get(light_id)
            if entry is None:
                return
            cookie = self._cookies.get(cookie_id)
            if cookie is None:
                raise CookieAtlasError("unknown cookie: %r" % (cookie_id,))
            self._validate_fit(cookie, entry.size, entry.kind)
            entry.cookie_id = cookie_id

    def slots(self):
        with self._lock:
            return {light_id: entry.slot
                    for light_id, entry in self._lights.items()}
