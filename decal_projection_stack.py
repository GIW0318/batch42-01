"""Decal ordering, atlas sampling and host lifecycle (skeleton).

Decals sort by layer and submission order, the atlas clamps sampling to one
cell, the limit fades old decals instead of dropping them and host changes
move their decals.  No behaviour is implemented yet.
"""

DECAL_LIMIT = "decal_limit"
UNKNOWN_DECAL = "unknown_decal"


class DecalError(Exception):
    def __init__(self, code, message=""):
        self.code = code
        super().__init__(code if not message else "%s: %s" % (code, message))


class DecalProjectionStack:
    def __init__(self, limit=32, atlas_grid=(4, 4), padding=1):
        self.limit = limit
        self.atlas_grid = atlas_grid
        self.padding = padding

    def add_decal(self, host_id, position, normal, layer=0,
                  atlas_cell=(0, 0), transparent=False, system_id="default",
                  decal_id=None):
        raise NotImplementedError

    def decal_order(self):
        raise NotImplementedError

    def visible_decals(self):
        raise NotImplementedError

    def remove_decal(self, decal_id):
        raise NotImplementedError

    def sample_atlas(self, cell, u, v, mip=0):
        raise NotImplementedError

    def atlas_cell_of_sample(self, cell, u, v):
        raise NotImplementedError

    def set_limit(self, limit):
        raise NotImplementedError

    def fade_state(self, decal_id):
        raise NotImplementedError

    def clear_scene(self):
        raise NotImplementedError

    def host_destroyed(self, host_id):
        raise NotImplementedError

    def update_host(self, host_id, position, normal):
        raise NotImplementedError

    def decal_transform(self, decal_id):
        raise NotImplementedError

    def atlas_cell_size(self):
        raise NotImplementedError

    def visible_from(self, camera_position):
        raise NotImplementedError

    def depth_tested(self, decal_id):
        raise NotImplementedError
