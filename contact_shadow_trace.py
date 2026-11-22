import math


class ContactShadowError(Exception):
    """Raised when contact-shadow parameters or inputs are invalid."""


STEP_PX_CAP = 16.0
MAX_DISTANCE_CAP = 1000.0
THICKNESS_CAP = 1.0
GAP_RATIO = 0.5
SUBSAMPLE_OFFSET = 0.25
EPS = 1e-6


def _is_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _require_positive(name, value, minimum=EPS):
    if not _is_number(value) or value < minimum:
        raise ContactShadowError(
            "%s must be a number >= %s, got %r" % (name, minimum, value))


class ContactShadowTrace:
    """Screen-space contact shadow ray marcher.

    Rays are marched in view space along the (screen projection of the)
    light direction. Only the current-frame depth buffer is read, paired
    with a normal buffer of identical layout. Depth discontinuities stop
    the ray so shadows cannot leak through walls, and every parameter is
    clamped to an explicit upper bound.
    """

    def __init__(self, max_steps=16, step_px=2.0, max_distance=100.0,
                 thickness=1.0):
        _require_positive("max_steps", max_steps, minimum=1.0)
        _require_positive("step_px", step_px)
        _require_positive("max_distance", max_distance)
        _require_positive("thickness", thickness)

        self.max_steps = int(max_steps)
        self.step_px = min(float(step_px), STEP_PX_CAP)
        self.max_distance = min(float(max_distance), MAX_DISTANCE_CAP)
        self.thickness = min(float(thickness), THICKNESS_CAP)

        self._budget = None
        self._frame_used = 0
        self._jitter_seed = 7
        self._last_stats = {}

    def trace(self, uv, depth_buffer, normal_buffer, light_dir, camera_dir,
              height=0.0, mask=1.0):
        """March a view-space ray and return occlusion in the range [0, 1]."""
        depth_buffer, normal_buffer = self._validate_inputs(
            uv, depth_buffer, normal_buffer, light_dir, camera_dir,
            height, mask)
        if not _is_number(mask) or mask <= 0.0:
            return 0.0
        if _is_number(height) and height > self.thickness:
            return 0.0

        light_x = float(light_dir[0])
        light_y = float(light_dir[1])
        if abs(light_x) < EPS and abs(light_y) < EPS:
            return 0.0

        count = len(depth_buffer)
        start = int(math.floor(float(uv[0]) * count + SUBSAMPLE_OFFSET))
        start = min(max(start, 0), count - 1)
        origin_depth = float(depth_buffer[start])

        step_limit, stride, downsampled = self._resolve_budget()
        direction = -1.0 if light_x >= 0.0 else 1.0
        angle_deg = math.degrees(math.atan2(abs(light_y), abs(light_x)))

        occlusion = 0.0
        performed = 0
        hit = False
        for step_index in range(1, step_limit + 1):
            sample_index = int(
                start + direction * stride * step_index)
            if sample_index < 0 or sample_index >= count:
                break
            performed = step_index

            sample_depth = float(depth_buffer[sample_index])
            penetration = origin_depth - sample_depth
            if penetration <= 0.0:
                continue
            if penetration <= self.thickness:
                hit = True
            elif penetration / max(origin_depth, EPS) > GAP_RATIO:
                break
            else:
                hit = True

            if hit:
                distance = step_index * self.step_px
                occlusion = self.sample_occlusion(
                    distance, self.thickness, angle_deg,
                    height if _is_number(height) else 0.0)
                occlusion *= min(max(float(mask), 0.0), 1.0)
                break

        if self._budget is not None:
            self._frame_used += performed
        self._last_stats = {
            "steps": performed,
            "step_limit": step_limit,
            "downsampled": downsampled,
            "stride_px": stride,
            "budget": self._budget,
            "frame_used": self._frame_used,
            "hit": hit,
        }
        return max(0.0, min(occlusion, 1.0))

    def sample_occlusion(self, distance, thickness, angle_deg, height=0.0):
        """Fade occlusion by travel distance, thickness and light angle."""
        if not _is_number(distance) or distance < 0.0:
            raise ContactShadowError("distance must be a non-negative number")
        if not _is_number(thickness) or thickness < 0.0:
            raise ContactShadowError("thickness must be a non-negative number")
        if not _is_number(angle_deg):
            raise ContactShadowError("angle_deg must be a number")
        if not _is_number(height) or height < 0.0:
            raise ContactShadowError("height must be a non-negative number")

        distance_fade = max(
            0.0, 1.0 - float(distance) / max(self.max_distance, EPS))
        thickness_fade = min(max(float(thickness), 0.0), THICKNESS_CAP)
        angle = min(max(float(angle_deg), 0.0), 90.0)
        angle_fade = max(0.0, min(math.sin(math.radians(angle)), 1.0))
        if height > 0.0:
            height_fade = max(
                0.0, 1.0 - float(height) / max(float(thickness), EPS))
        else:
            height_fade = 1.0
        return max(
            0.0,
            min(distance_fade * thickness_fade * angle_fade * height_fade,
                1.0))

    def set_budget(self, max_steps_per_frame):
        """Cap total ray-march steps for the upcoming frame."""
        if max_steps_per_frame is None:
            self._budget = None
            self._frame_used = 0
            return
        _require_positive("max_steps_per_frame", max_steps_per_frame,
                          minimum=1.0)
        self._budget = int(max_steps_per_frame)
        self._frame_used = 0

    def begin_frame(self):
        """Reset per-frame step accounting."""
        self._frame_used = 0

    def jitter(self, frame):
        """Deterministic per-frame jitter from a fixed seed.

        The value depends only on the frame index, never on camera
        position or orientation, so camera motion cannot swap noise.
        """
        if not _is_number(frame):
            raise ContactShadowError("frame must be a number")
        hashed = (self._jitter_seed * 1000003) ^ (int(frame) & 0xFFFFFFFF)
        hashed = (hashed ^ (hashed >> 13)) * 1274126177 & 0xFFFFFFFF
        hashed ^= hashed >> 16
        return float(hashed & 0xFFFF) / 65535.0

    def combine(self, contact, cascaded):
        """Take the strongest shadow instead of stacking them."""
        if not _is_number(contact) or not _is_number(cascaded):
            raise ContactShadowError("shadow values must be numbers")
        return max(0.0,
                   min(max(float(contact), float(cascaded)), 1.0))

    def params(self, eye="left"):
        """Return identical sampling parameters for both VR eyes."""
        return {
            "max_steps": self.max_steps,
            "step_px": self.step_px,
            "max_distance": self.max_distance,
            "thickness": self.thickness,
            "jitter_seed": self._jitter_seed,
        }

    def last_stats(self):
        return dict(self._last_stats)

    def _resolve_budget(self):
        base_stride = max(1, int(self.step_px))
        if self._budget is None:
            return self.max_steps, base_stride, False

        remaining = self._budget - self._frame_used
        if remaining <= 0:
            self._frame_used = 0
            remaining = self._budget
        step_limit = min(self.max_steps, max(1, int(remaining)))
        if step_limit >= self.max_steps:
            return step_limit, base_stride, False

        scale = int(math.ceil(self.max_steps / float(step_limit)))
        return step_limit, base_stride * scale, True

    def _validate_inputs(self, uv, depth_buffer, normal_buffer, light_dir,
                        camera_dir, height, mask):
        if not hasattr(depth_buffer, "__len__") or \
                not hasattr(normal_buffer, "__len__"):
            raise ContactShadowError("depth and normal buffers must be "
                                     "indexable sequences")
        if len(depth_buffer) == 0 or len(depth_buffer) != len(normal_buffer):
            raise ContactShadowError(
                "depth and normal buffers must be non-empty and share one "
                "layout")
        if not hasattr(uv, "__len__") or len(uv) != 2 or \
                not all(_is_number(component) for component in uv) or \
                not (0.0 <= float(uv[0]) <= 1.0) or \
                not (0.0 <= float(uv[1]) <= 1.0):
            raise ContactShadowError("uv must be a (u, v) pair in [0, 1]")
        for name, vector in (("light_dir", light_dir),
                             ("camera_dir", camera_dir)):
            if not hasattr(vector, "__len__") or len(vector) != 3 or \
                    not all(_is_number(component) for component in vector):
                raise ContactShadowError(
                    "%s must be a 3-component numeric vector" % name)
        if not _is_number(height) or height < 0.0:
            raise ContactShadowError("height must be a non-negative number")
        if not _is_number(mask) or mask < 0.0:
            raise ContactShadowError("mask must be a non-negative number")
        if any(not _is_number(value) for value in depth_buffer):
            raise ContactShadowError("depth buffer entries must be numbers")
        return depth_buffer, normal_buffer
