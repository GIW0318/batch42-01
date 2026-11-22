"""Terrain splat weight normalization, sampling and LOD seams.

The first four layers are normalized to one, out-of-range layers fall back to
the default layer, LOD blends stay continuous at the same world position and
seam checks compare both blocks.
"""

import math


TERRAIN_SAMPLING = "terrain_sampling"
LAYER_OUT_OF_RANGE = "layer_out_of_range"


def _finite_float(value, fallback=0.0):
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return fallback
    if not math.isfinite(result):
        return fallback
    return result


def _non_negative(value, fallback=0.0):
    result = _finite_float(value, fallback)
    return result if result > 0.0 else 0.0


def _clamp(value, low=0.0, high=1.0):
    return min(high, max(low, value))


def _smoothstep(value, low, high):
    if high <= low:
        return 1.0 if value >= high else 0.0
    t = _clamp((value - low) / (high - low))
    return t * t * (3.0 - 2.0 * t)


class TerrainError(Exception):
    def __init__(self, code, message=""):
        self.code = code
        super().__init__(code if not message else "%s: %s" % (code, message))


class TerrainSplatBlend:
    """CPU-side reference model for terrain splatting and LOD decisions."""

    NEAR_DISTANCE = 10.0
    FAR_DISTANCE = 200.0
    SNOW_LINE_LOW = 60.0
    SNOW_LINE_HIGH = 100.0

    LAYER_COLORS = (
        (0.24, 0.55, 0.18),
        (0.32, 0.63, 0.21),
        (0.52, 0.43, 0.27),
        (0.44, 0.45, 0.47),
    )

    def __init__(self, layer_count=16, default_layer=0, samples_near=12,
                 samples_far=3, lod_threshold=0.02, height_blend=True,
                 default_color=(0.5, 0.5, 0.5)):
        self.layer_count = layer_count
        self.default_layer = default_layer
        self.samples_near = samples_near
        self.samples_far = samples_far
        self.lod_threshold = lod_threshold
        self.height_blend = height_blend
        self.default_color = default_color

        self._lod_distance = 0.0
        self._streamed = True
        self._holes = []

    # ------------------------------------------------------------------
    # Weight handling
    # ------------------------------------------------------------------
    def normalize_weights(self, weights):
        """Normalize four weights and fold extra layers into the maximum one.

        Negative, NaN and infinite inputs are treated as zero.  This mirrors
        half-float weight maps where values very close to zero may arrive as
        signed garbage and would otherwise darken the terrain.
        """
        try:
            source = list(weights)
        except TypeError:
            source = []

        values = [_non_negative(value) for value in source]
        if not values:
            values = [0.0]

        base_values = list(values[:4])
        if len(base_values) < 4:
            base_values.extend([0.0] * (4 - len(base_values)))
        base_total = sum(base_values)
        if base_total > 0.0:
            target = max(range(4), key=lambda index: base_values[index])
        else:
            target = self._default_slot()

        largest = max(values)
        if largest > 0.0:
            values = [value / largest for value in values]

        base = list(values[:4])
        if len(base) < 4:
            base.extend([0.0] * (4 - len(base)))
        overflow = values[4:]

        if overflow:
            base[target] += sum(overflow)

        total = sum(base)
        if not math.isfinite(total) or total <= 0.0:
            base = [0.0, 0.0, 0.0, 0.0]
            base[target] = 1.0
            return base

        result = [value / total for value in base]

        # Remove the tiny residue produced by floating point division so the
        # four weights always add up to exactly one.
        residue = 1.0 - sum(result)
        if residue:
            result[target] += residue
        return result

    def sample_layer(self, layer_index):
        """Return a valid texture-array layer or the configured fallback.

        Texture arrays are never allowed to wrap around: a seventeenth layer
        with a sixteen-layer array must not sample layer zero by accident.
        """
        count = self._layer_count()
        if count <= 0:
            return 0

        try:
            if isinstance(layer_index, str):
                raise ValueError
            number = float(layer_index)
            index = int(number)
            if not math.isfinite(number) or number != index:
                raise ValueError
        except (TypeError, ValueError, OverflowError):
            return self._default_layer_index()

        if 0 <= index < count:
            return index
        return self._default_layer_index()

    def quantize_weights(self, weights, bits=8):
        """Quantize to a fixed-point map while preserving the unit sum."""
        try:
            bit_count = int(bits)
        except (TypeError, ValueError, OverflowError):
            raise ValueError("bits must be a positive integer")
        if bit_count <= 0:
            raise ValueError("bits must be a positive integer")

        normalized = self.normalize_weights(weights)
        levels = (1 << bit_count) - 1
        scaled = [weight * levels for weight in normalized]
        quantized = [int(math.floor(value)) for value in scaled]

        used = sum(quantized)
        if used > levels:
            excess = used - levels
            order = sorted(range(4), key=lambda index: quantized[index])
            while excess:
                for index in order:
                    if excess and quantized[index] > 0:
                        quantized[index] -= 1
                        excess -= 1
        elif used < levels:
            remainder = [scaled[index] - quantized[index]
                         for index in range(4)]
            for _ in range(levels - used):
                index = max(range(4),
                            key=lambda item: (remainder[item], -item))
                quantized[index] += 1
                remainder[index] = -1.0

        return [value / levels for value in quantized]

    # ------------------------------------------------------------------
    # Sampling and LOD
    # ------------------------------------------------------------------
    def blend_at(self, world_x, world_z, lod=None):
        """Sample the splat at a world-space position.

        The analytic weights depend only on world position, never directly on
        the selected LOD.  Consequently the same world coordinate produces the
        same colour, height and normal on every LOD level.
        """
        x = _finite_float(world_x)
        z = _finite_float(world_z)
        distance, requested_lod = self._resolve_lod(lod)
        clamped_x = self._clamp_hole_x(x)

        weights = self._weights_at(clamped_x, z)
        height = self._height_at(clamped_x, z)
        normal = self._normal_at(clamped_x, z)
        default_color = self._default_color()

        if self._streamed:
            color = tuple(
                sum(weights[index] * self.LAYER_COLORS[index][channel]
                    for index in range(4))
                for channel in range(3)
            )
            status = "ready"
        else:
            color = default_color
            status = "streaming"

        samples = self.sample_count(distance)
        near_samples = self._sample_limit(self.samples_near, 1)
        height_alpha = 0.0
        if self.height_blend:
            height_alpha = _clamp(max(weights))

        return {
            "weights": weights,
            "color": color,
            "height_alpha": height_alpha,
            "height": height,
            "normal": normal,
            "status": status,
            "lod": (requested_lod if requested_lod is not None
                    else self._lod_level(distance)),
            "distance": distance,
            "samples_per_pixel": samples,
            "degraded": samples < near_samples,
        }

    def set_lod(self, distance):
        self._lod_distance = _non_negative(distance)

    def sample_count(self, distance=None):
        """Return the per-pixel sample budget for a camera distance."""
        near = self._sample_limit(self.samples_near, 1)
        far = self._sample_limit(self.samples_far, 1)
        far = min(far, near)

        current = self._distance(distance)
        if current <= self.NEAR_DISTANCE:
            return near
        if current >= self.FAR_DISTANCE:
            return far

        span = self.FAR_DISTANCE - self.NEAR_DISTANCE
        t = (current - self.NEAR_DISTANCE) / span
        count = int(round(near + (far - near) * t))
        return max(far, min(near, count))

    def budget_report(self, distance=None):
        current = self._distance(distance)
        samples = self.sample_count(current)
        near = self._sample_limit(self.samples_near, 1)
        degraded = samples < near
        return {
            "samples_per_pixel": samples,
            "degraded": degraded,
            "within_budget": not degraded,
            "distance": current,
            "lod": self._lod_level(current),
        }

    # ------------------------------------------------------------------
    # Seam validation
    # ------------------------------------------------------------------
    def seam_check(self, block_a, block_b):
        """Compare the facing edges of two terrain blocks."""
        tolerance = _non_negative(self.lod_threshold, 0.02)

        weights_a = self._seam_weights(block_a, right_side=True)
        weights_b = self._seam_weights(block_b, right_side=False)
        weight_delta = self._weight_delta(weights_a, weights_b)

        normal_a = self._seam_normal(block_a, right_side=True)
        normal_b = self._seam_normal(block_b, right_side=False)
        normal_delta = self._normal_delta(normal_a, normal_b)

        height_a = self._seam_height(block_a, right_side=True)
        height_b = self._seam_height(block_b, right_side=False)
        height_delta = abs(height_a - height_b)

        weight_ok = weight_delta <= tolerance
        normal_ok = normal_delta <= tolerance
        height_ok = height_delta <= tolerance
        continuous = weight_ok and normal_ok and height_ok

        return {
            "continuous": continuous,
            "weight_delta": weight_delta,
            "normal_delta": normal_delta,
            "height_delta": height_delta,
            "weights_match": weight_ok,
            "normal_match": normal_ok,
            "height_match": height_ok,
            "threshold": tolerance,
        }

    # ------------------------------------------------------------------
    # Holes, streaming and collision
    # ------------------------------------------------------------------
    def clamp_uv(self, u, v):
        """Clamp a UV to the nearest valid texel in the [0, 1] tile."""
        return (
            _clamp(_finite_float(u), 0.0, 1.0),
            _clamp(_finite_float(v), 0.0, 1.0),
        )

    def deform_hole(self, center_x, radius):
        center = _finite_float(center_x)
        hole_radius = _non_negative(radius)
        if hole_radius <= 0.0:
            return {"center_x": center, "radius": 0.0,
                    "holes": len(self._holes)}

        self._holes.append((center - hole_radius,
                            center + hole_radius))
        return {"center_x": center, "radius": hole_radius,
                "holes": len(self._holes)}

    def collision_height(self, x):
        position = _finite_float(x)
        if self._inside_hole(position):
            return None
        valid_x = self._clamp_hole_x(position)
        return self._height_at(valid_x, 0.0)

    def set_streamed(self, loaded):
        self._streamed = bool(loaded)

    # ------------------------------------------------------------------
    # Material helpers
    # ------------------------------------------------------------------
    def snow_coverage(self, x, z, slope_normal, world_height):
        """Snow depends on world height, not on the slope facing direction."""
        height = _finite_float(world_height)
        return _smoothstep(height, self.SNOW_LINE_LOW, self.SNOW_LINE_HIGH)

    def decal_anchor(self, x, z, lod):
        """Return a stable terrain-space anchor for a decal."""
        position_x = self._clamp_hole_x(_finite_float(x))
        position_z = _finite_float(z)
        return self._height_at(position_x, position_z)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    def _layer_count(self):
        try:
            return max(0, int(self.layer_count))
        except (TypeError, ValueError, OverflowError):
            return 0

    def _default_slot(self):
        try:
            layer = int(self.default_layer)
        except (TypeError, ValueError, OverflowError):
            return 0
        return layer if 0 <= layer < 4 else 0

    def _default_layer_index(self):
        count = self._layer_count()
        if count <= 0:
            return 0
        try:
            layer = int(self.default_layer)
        except (TypeError, ValueError, OverflowError):
            return 0
        return min(count - 1, max(0, layer))

    def _distance(self, distance=None):
        if distance is None:
            return _non_negative(self._lod_distance)
        return _non_negative(distance)

    def _resolve_lod(self, lod):
        """Accept either an LOD level or a world-space distance."""
        if lod is None:
            return _non_negative(self._lod_distance), None

        value = _finite_float(lod, -1.0)
        if value < 0.0:
            return 0.0, None

        # The public LOD levels are 0..2.  Larger values are interpreted as a
        # distance so callers can also pass the same value as set_lod().
        if value == int(value) and 0 <= int(value) <= 2:
            level = int(value)
            distance = self.FAR_DISTANCE * (level / 2.0)
            return distance, level
        return value, None

    def _sample_limit(self, value, fallback):
        try:
            result = int(round(float(value)))
        except (TypeError, ValueError, OverflowError):
            return fallback
        return max(1, result)

    def _lod_level(self, distance):
        near = self._sample_limit(self.samples_near, 1)
        far = min(self._sample_limit(self.samples_far, 1), near)
        samples = self.sample_count(distance)
        if samples >= near:
            return 0
        if samples <= far:
            return 2
        return 1

    def _weights_at(self, x, z):
        raw = [
            0.50 + 0.50 * math.sin(0.11 * x + 0.07 * z + 0.40),
            0.50 + 0.50 * math.cos(0.09 * x - 0.13 * z - 0.20),
            0.50 + 0.50 * math.sin(0.05 * (x + z) + 1.70),
            0.50 + 0.50 * math.cos(0.08 * (x - z) - 0.40),
        ]
        return self.normalize_weights(raw)

    def _height_at(self, x, z):
        return (5.0
                + 0.60 * math.sin(0.17 * x)
                + 0.35 * math.cos(0.11 * z)
                + 0.15 * math.sin(0.07 * (x + z)))

    def _normal_at(self, x, z):
        dx = (0.60 * 0.17 * math.cos(0.17 * x)
              + 0.15 * 0.07 * math.cos(0.07 * (x + z)))
        dz = (-0.35 * 0.11 * math.sin(0.11 * z)
              + 0.15 * 0.07 * math.cos(0.07 * (x + z)))
        length = math.sqrt(dx * dx + 1.0 + dz * dz)
        return (-dx / length, 1.0 / length, -dz / length)

    def _default_color(self):
        try:
            color = tuple(_finite_float(channel, 0.5)
                          for channel in self.default_color)
        except TypeError:
            return (0.5, 0.5, 0.5)
        if len(color) != 3:
            return (0.5, 0.5, 0.5)
        return color

    def _merged_holes(self):
        if not self._holes:
            return []
        ordered = sorted(self._holes)
        merged = [list(ordered[0])]
        for low, high in ordered[1:]:
            if low <= merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], high)
            else:
                merged.append([low, high])
        return [(low, high) for low, high in merged]

    def _inside_hole(self, x):
        return any(low <= x <= high
                   for low, high in self._merged_holes())

    def _clamp_hole_x(self, x):
        for low, high in self._merged_holes():
            if low <= x <= high:
                if (x - low) <= (high - x):
                    return low
                return high
        return x

    def _block_value(self, block, key, default=None):
        if isinstance(block, dict):
            return block.get(key, default)
        return getattr(block, key, default)

    def _seam_weights(self, block, right_side):
        value = self._block_value(block, "weights")
        if value is None:
            value = self._block_value(block, "weight_map")
        if value is None:
            value = self._block_value(block, "weight_maps")
        if value is None:
            return [self.normalize_weights([0.0, 0.0, 0.0, 0.0])]

        if isinstance(value, dict):
            keys = list(value.keys())
            try:
                keys.sort(key=lambda item: int(item))
            except (TypeError, ValueError):
                pass
            if any(self._is_sequence(value[key]) for key in keys):
                edge = []
                for key in keys:
                    layer_values = value[key]
                    if self._is_sequence(layer_values):
                        layer_values = list(layer_values)
                        if layer_values:
                            edge.append(layer_values[-1] if right_side
                                        else layer_values[0])
                        else:
                            edge.append(0.0)
                    else:
                        edge.append(layer_values)
                return [self.normalize_weights(edge)]
            return [self.normalize_weights([value[key] for key in keys])]

        # A nested map is compared along its facing edge.  Flat arrays are
        # either one four-layer sample or a sequence of samples.
        if self._is_sequence(value):
            items = list(value)
            if items and self._is_sequence(items[0]):
                rows = [list(row) for row in items if self._is_sequence(row)]
                if not rows:
                    return [self.normalize_weights([0.0, 0.0, 0.0, 0.0])]
                if len(rows) == 4 and all(len(row) != 4 for row in rows):
                    edge = [row[-1] if right_side else row[0]
                            for row in rows]
                    return [self.normalize_weights(edge)]
                row = rows[-1] if right_side else rows[0]
                if len(row) == 4:
                    return [self.normalize_weights(row)]
                if len(row) > 4 and len(row) % 4 == 0:
                    edge = row[-4:] if right_side else row[:4]
                    return [self.normalize_weights(edge)]
                column = -1 if right_side else 0
                return [self.normalize_weights([row[column]])]

            numbers = [_non_negative(item) for item in items]
            if len(numbers) == 4:
                return [self.normalize_weights(numbers)]
            if len(numbers) > 4 and len(numbers) % 4 == 0:
                chunks = [numbers[index:index + 4]
                          for index in range(0, len(numbers), 4)]
                edge = chunks[-1] if right_side else chunks[0]
                return [self.normalize_weights(edge)]
            return [self.normalize_weights(numbers[:4])]

        return [self.normalize_weights([value])]

    def _weight_delta(self, samples_a, samples_b):
        if not samples_a and not samples_b:
            return 0.0
        if len(samples_a) != len(samples_b):
            return 1.0
        delta = 0.0
        for sample_a, sample_b in zip(samples_a, samples_b):
            for value_a, value_b in zip(sample_a, sample_b):
                delta = max(delta, abs(value_a - value_b))
        return delta

    def _seam_vector(self, block, key, right_side):
        value = self._block_value(block, key)
        if value is None:
            value = self._block_value(block, key + "s")
        if value is None:
            return None
        if self._is_sequence(value):
            items = list(value)
            if len(items) == 3 and all(not self._is_sequence(item)
                                       for item in items):
                return tuple(_finite_float(item) for item in items)
            if items and self._is_sequence(items[0]):
                item = items[-1] if right_side else items[0]
                return self._as_vector(item)
        return None

    def _as_vector(self, value):
        if not self._is_sequence(value):
            return None
        items = list(value)
        if len(items) != 3:
            return None
        return tuple(_finite_float(item) for item in items)

    def _seam_normal(self, block, right_side):
        value = self._seam_vector(block, "normal", right_side)
        if value is None:
            return (0.0, 0.0, 1.0)
        length = math.sqrt(sum(component * component
                               for component in value))
        if length <= 0.0:
            return (0.0, 0.0, 0.0)
        return tuple(component / length for component in value)

    def _normal_delta(self, normal_a, normal_b):
        return max(abs(a - b) for a, b in zip(normal_a, normal_b))

    def _seam_height(self, block, right_side):
        value = self._block_value(block, "height")
        if value is None:
            value = self._block_value(block, "heights")
        if self._is_sequence(value):
            items = list(value)
            if items and self._is_sequence(items[0]):
                item = items[-1] if right_side else items[0]
                items = list(item)
            if items:
                value = items[-1] if right_side else items[0]
            else:
                value = 0.0
        return _finite_float(value)

    def _is_sequence(self, value):
        return isinstance(value, (list, tuple))
