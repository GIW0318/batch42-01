"""Impostor direction baking, atlas layout and LOD switching (skeleton).

Frames cover yaw and pitch, the atlas keeps a gutter per cell, instance
colour is applied once and only one representation is visible per distance.
No behaviour is implemented yet.
"""

GEOMETRY_CLIPPED = "geometry_clipped"
IMPOSTOR_SETUP = "impostor_setup"


class ImpostorError(Exception):
    def __init__(self, code, message=""):
        self.code = code
        super().__init__(code if not message else "%s: %s" % (code, message))


class ImpostorBillboardBake:
    def __init__(self, directions=8, atlas_grid=(4, 4), switch_distance=50.0,
                 padding=2, mip_levels=3):
        self.directions = directions
        self.atlas_grid = atlas_grid
        self.switch_distance = switch_distance
        self.padding = padding
        self.mip_levels = mip_levels

    def bake(self, tree_id, near_plane=0.1, raise_on_clip=True):
        raise NotImplementedError

    def direction_frame(self, yaw, pitch):
        raise NotImplementedError

    def sample(self, yaw, pitch):
        raise NotImplementedError

    def atlas_layout(self):
        raise NotImplementedError

    def color_at(self, yaw, pitch):
        raise NotImplementedError

    def near_color(self):
        raise NotImplementedError

    def instance_color(self, base, tint):
        raise NotImplementedError

    def set_lod_distance(self, distance):
        raise NotImplementedError

    def visible_models(self, distance):
        raise NotImplementedError

    def budget_report(self):
        raise NotImplementedError

    def normal_at(self, yaw, pitch):
        raise NotImplementedError

    def color_in_shadow(self, yaw, pitch):
        raise NotImplementedError

    def apply_fog(self, color, fog_density):
        raise NotImplementedError

    def set_instance_count(self, count):
        raise NotImplementedError
