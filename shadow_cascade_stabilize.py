"""Cascaded shadow split, texel stabilization and atlas.

Cascade splits come from the camera frustum ratio, camera translation snaps
to whole texels, blends use world-space widths and invalid setups raise a
setup error naming the config item.
"""

import math

SHADOW_SETUP = "shadow_setup"

_DEFAULT_NEAR = 0.1
_DEFAULT_FAR = 200.0
_BASE_CASCADE_EXTENT = 8.0
_CONTACT_RANGE = 2.0


class ShadowError(Exception):
    def __init__(self, code, message=""):
        self.code = code
        super().__init__(code if not message else "%s: %s" % (code, message))


class ShadowSetupError(ShadowError):
    pass


class CascadeStabilize:
    def __init__(self, cascade_count=3, shadow_map_size=2048,
                 light_dir=(-0.3, -1.0, -0.4), split_lambda=0.5,
                 blend_width=5.0, atlas_slots=4, update_budget=2):
        self.cascade_count = cascade_count
        self.shadow_map_size = shadow_map_size
        self.light_dir = light_dir
        self.split_lambda = split_lambda
        self.blend_width = blend_width
        self.atlas_slots = atlas_slots
        self.update_budget = update_budget
        self.contact_range = _CONTACT_RANGE
        self._splits = None
        self._updated_this_frame = 0
        self._deferred_this_frame = 0
        self._validate()

    def _validate(self):
        if (not isinstance(self.cascade_count, int)
                or self.cascade_count < 1):
            raise ShadowSetupError(
                SHADOW_SETUP,
                "cascade_count must be a positive integer, got %r"
                % (self.cascade_count,))
        if self.shadow_map_size <= 0:
            raise ShadowSetupError(
                SHADOW_SETUP,
                "shadow_map_size must be positive, got %r"
                % (self.shadow_map_size,))
        if self._light_length() <= 0.0:
            raise ShadowSetupError(
                SHADOW_SETUP,
                "light_dir is degenerate (zero length): %r"
                % (self.light_dir,))
        if not 0.0 < self.split_lambda < 1.0:
            raise ShadowSetupError(
                SHADOW_SETUP,
                "split_lambda must be in (0, 1), got %r"
                % (self.split_lambda,))
        if self.blend_width <= 0.0:
            raise ShadowSetupError(
                SHADOW_SETUP,
                "blend_width must be positive, got %r"
                % (self.blend_width,))
        if self.atlas_slots < 1:
            raise ShadowSetupError(
                SHADOW_SETUP,
                "atlas_slots must be positive, got %r"
                % (self.atlas_slots,))
        if self.update_budget < 1:
            raise ShadowSetupError(
                SHADOW_SETUP,
                "update_budget must be positive, got %r"
                % (self.update_budget,))

    def _light_length(self):
        return math.sqrt(sum(component * component
                             for component in self.light_dir))

    def _check_cascade_index(self, cascade_index):
        if not 0 <= cascade_index < self.cascade_count:
            raise ShadowSetupError(
                SHADOW_SETUP,
                "cascade_index %r out of range for cascade_count=%d"
                % (cascade_index, self.cascade_count))

    def _check_light_id(self, light_id):
        if not 0 <= light_id < self.atlas_slots:
            raise ShadowSetupError(
                SHADOW_SETUP,
                "light_id %r has no atlas region; atlas_slots=%d"
                % (light_id, self.atlas_slots))

    def configure(self, **kwargs):
        for key, value in kwargs.items():
            if not hasattr(self, key):
                raise ShadowSetupError(
                    SHADOW_SETUP, "unknown config item %r" % (key,))
            setattr(self, key, value)
        self._validate()
        self._splits = None

    def split_distances(self, near, far):
        if near <= 0.0:
            raise ShadowSetupError(
                SHADOW_SETUP, "near must be positive, got %r" % (near,))
        if far <= near:
            raise ShadowSetupError(
                SHADOW_SETUP,
                "far must be greater than near, got far=%r near=%r"
                % (far, near))
        count = self.cascade_count
        splits = []
        for index in range(1, count + 1):
            fraction = index / count
            logarithmic = near * (far / near) ** fraction
            linear = near + (far - near) * fraction
            splits.append(self.split_lambda * logarithmic
                          + (1.0 - self.split_lambda) * linear)
        splits[-1] = far
        self._splits = splits
        return list(splits)

    def light_matrix(self, cascade_index):
        self._check_cascade_index(cascade_index)
        length = self._light_length()
        dir_x, dir_y, dir_z = (component / length
                               for component in self.light_dir)
        up_x, up_y, up_z = 0.0, 1.0, 0.0
        if abs(dir_y) > 0.999:
            up_x, up_y, up_z = 1.0, 0.0, 0.0
        right_x = dir_y * up_z - dir_z * up_y
        right_y = dir_z * up_x - dir_x * up_z
        right_z = dir_x * up_y - dir_y * up_x
        right_len = math.sqrt(right_x ** 2 + right_y ** 2 + right_z ** 2)
        right_x, right_y, right_z = (right_x / right_len,
                                     right_y / right_len,
                                     right_z / right_len)
        up_x = right_y * dir_z - right_z * dir_y
        up_y = right_z * dir_x - right_x * dir_z
        up_z = right_x * dir_y - right_y * dir_x
        extent = self._cascade_extent(cascade_index)
        scale = 2.0 / extent
        depth_scale = 1.0 / (extent * self.cascade_count)
        return (
            right_x * scale, up_x * scale, dir_x * depth_scale, 0.0,
            right_y * scale, up_y * scale, dir_y * depth_scale, 0.0,
            right_z * scale, up_z * scale, dir_z * depth_scale, 0.0,
            0.0, 0.0, 0.5, 1.0,
        )

    def _cascade_extent(self, cascade_index):
        return _BASE_CASCADE_EXTENT * (2.0 ** cascade_index)

    def stabilize(self, cascade_index, camera_position):
        self._check_cascade_index(cascade_index)
        texel = self.texel_size(cascade_index)
        return tuple(round(component / texel) * texel
                     for component in camera_position)

    def texel_size(self, cascade_index):
        self._check_cascade_index(cascade_index)
        return self._cascade_extent(cascade_index) / self.shadow_map_size

    def blend(self, world_distance):
        splits = self._splits
        if splits is None:
            splits = self.split_distances(_DEFAULT_NEAR, _DEFAULT_FAR)
        boundaries = splits[:-1]
        boundary = min(boundaries,
                       key=lambda candidate: abs(candidate - world_distance))
        factor = 0.5 + (world_distance - boundary) / (2.0 * self.blend_width)
        return min(1.0, max(0.0, factor))

    def atlas_region(self, light_id):
        self._check_light_id(light_id)
        columns = math.ceil(math.sqrt(self.atlas_slots))
        rows = math.ceil(self.atlas_slots / columns)
        column = light_id % columns
        row = light_id // columns
        width = 1.0 / columns
        height = 1.0 / rows
        return (column * width, row * height, width, height)

    def update_light(self, light_id):
        self._check_light_id(light_id)
        if self._updated_this_frame < self.update_budget:
            self._updated_this_frame += 1
            status = "updated"
        else:
            self._deferred_this_frame += 1
            status = "deferred"
        return {"light_id": light_id, "status": status,
                "atlas_region": self.atlas_region(light_id)}

    def begin_frame(self):
        self._updated_this_frame = 0
        self._deferred_this_frame = 0

    def budget_report(self):
        return {"budget": self.update_budget,
                "updated_lights": self._updated_this_frame,
                "deferred_lights": self._deferred_this_frame}

    def contact_shadow(self, distance):
        if distance < 0.0:
            distance = 0.0
        return max(0.0, 1.0 - distance / self.contact_range)

    def shadow_strength(self, opacity):
        return min(1.0, max(0.0, opacity))
