class OutlineSetupError(Exception):
    pass


class ToonOutline:
    def __init__(self, width_px=2.0, base_height=1080,
                 resolution=(1920, 1080)):
        self.width_px = width_px
        self.base_height = base_height
        self.resolution = resolution
        self._normal_map = None
        self._normals = []
        self._quality = 1.0
        self._budget_ms = None
        self._mask = []

    def set_resolution(self, width, height):
        raise NotImplementedError()

    def effective_width(self):
        raise NotImplementedError()

    def compute_width(self, distance):
        raise NotImplementedError()

    def classify_edge(self, normal_a, normal_b, depth_a, depth_b,
                      hard_edge=False):
        raise NotImplementedError()

    def set_normals(self, normals):
        raise NotImplementedError()

    def smooth_normals(self):
        raise NotImplementedError()

    def set_normal_map(self, data):
        raise NotImplementedError()

    def is_occluded(self, outline_depth, wall_depth):
        raise NotImplementedError()

    def vr_params(self):
        raise NotImplementedError()

    def apply_postprocess(self, mask):
        raise NotImplementedError()

    def coverage(self):
        raise NotImplementedError()

    def set_budget(self, budget_ms):
        raise NotImplementedError()

    def quality(self):
        raise NotImplementedError()

    def render_order(self):
        raise NotImplementedError()
