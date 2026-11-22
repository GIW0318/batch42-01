"""Terrain splat weight normalization, sampling and LOD seams (skeleton).

The first four layers are normalized to one, out-of-range layers fall back to
the default layer, LOD blends stay continuous at the same world position and
seam checks compare both blocks.  No behaviour is implemented yet.
"""

TERRAIN_SAMPLING = "terrain_sampling"
LAYER_OUT_OF_RANGE = "layer_out_of_range"


class TerrainError(Exception):
    def __init__(self, code, message=""):
        self.code = code
        super().__init__(code if not message else "%s: %s" % (code, message))


class TerrainSplatBlend:
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

    def normalize_weights(self, weights):
        raise NotImplementedError

    def sample_layer(self, layer_index):
        raise NotImplementedError

    def blend_at(self, world_x, world_z, lod=None):
        raise NotImplementedError

    def set_lod(self, distance):
        raise NotImplementedError

    def seam_check(self, block_a, block_b):
        raise NotImplementedError

    def clamp_uv(self, u, v):
        raise NotImplementedError

    def sample_count(self, distance=None):
        raise NotImplementedError

    def budget_report(self, distance=None):
        raise NotImplementedError

    def quantize_weights(self, weights, bits=8):
        raise NotImplementedError

    def deform_hole(self, center_x, radius):
        raise NotImplementedError

    def collision_height(self, x):
        raise NotImplementedError

    def set_streamed(self, loaded):
        raise NotImplementedError

    def snow_coverage(self, x, z, slope_normal, world_height):
        raise NotImplementedError

    def decal_anchor(self, x, z, lod):
        raise NotImplementedError
