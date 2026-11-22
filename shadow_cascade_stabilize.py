"""Cascaded shadow split, texel stabilization and atlas (skeleton).

Cascade splits come from the camera frustum ratio, camera translation snaps
to whole texels, blends use world-space widths and invalid setups raise a
setup error naming the config item.  No behaviour is implemented yet.
"""

SHADOW_SETUP = "shadow_setup"


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

    def configure(self, **kwargs):
        raise NotImplementedError

    def split_distances(self, near, far):
        raise NotImplementedError

    def light_matrix(self, cascade_index):
        raise NotImplementedError

    def stabilize(self, cascade_index, camera_position):
        raise NotImplementedError

    def texel_size(self, cascade_index):
        raise NotImplementedError

    def blend(self, world_distance):
        raise NotImplementedError

    def atlas_region(self, light_id):
        raise NotImplementedError

    def update_light(self, light_id):
        raise NotImplementedError

    def budget_report(self):
        raise NotImplementedError

    def contact_shadow(self, distance):
        raise NotImplementedError

    def shadow_strength(self, opacity):
        raise NotImplementedError
