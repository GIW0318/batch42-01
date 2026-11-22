class ReflectionSetupError(Exception):
    pass


class PlanarReflection:
    def __init__(self, width=1920, height=1080, max_recursion=1):
        self.width = width
        self.height = height
        self.max_recursion = max_recursion
        self._planes = {}
        self._targets = {}
        self._next_target = 1
        self._layers = None
        self._particles = True
        self._budget_ms = None
        self._wave_normals = {}

    def add_plane(self, plane_id, point, normal):
        raise NotImplementedError()

    def reflect_point(self, plane_id, point):
        raise NotImplementedError()

    def clip_plane(self, plane_id, camera_position):
        raise NotImplementedError()

    def render(self, plane_id, camera, objects):
        raise NotImplementedError()

    def set_resolution(self, width, height):
        raise NotImplementedError()

    def set_layers(self, mask):
        raise NotImplementedError()

    def set_particles(self, enabled):
        raise NotImplementedError()

    def set_budget(self, ms):
        raise NotImplementedError()

    def set_wave_normal(self, plane_id, normal):
        raise NotImplementedError()

    def uv_offset(self, plane_id, uv):
        raise NotImplementedError()

    def reset_scene(self):
        raise NotImplementedError()

    def targets(self):
        raise NotImplementedError()
