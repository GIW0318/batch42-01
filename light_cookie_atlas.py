class CookieAtlasError(Exception):
    pass


class CookieAtlas:
    def __init__(self, atlas_size=1024, cell_size=256, max_lights=4):
        self.atlas_size = atlas_size
        self.cell_size = cell_size
        self.max_lights = max_lights
        self._lights = {}
        self._slots = {}
        self._cookies = {}
        self._animations = {}
        self._budget_ms = None

    def register_light(self, light_id, cookie_id, size=256, kind="spot"):
        raise NotImplementedError()

    def release_light(self, light_id):
        raise NotImplementedError()

    def slot_of(self, light_id):
        raise NotImplementedError()

    def projection_matrix(self, light_id, position, direction, fov, near=0.1,
                          far=100.0, aspect=1.0):
        raise NotImplementedError()

    def set_cookie(self, cookie_id, colors, spot_only=False):
        raise NotImplementedError()

    def sample(self, light_id, uv):
        raise NotImplementedError()

    def set_animation(self, cookie_id, frames, fps):
        raise NotImplementedError()

    def frame_at(self, cookie_id, time_ms):
        raise NotImplementedError()

    def set_budget(self, ms):
        raise NotImplementedError()

    def update(self, lights):
        raise NotImplementedError()

    def slots(self):
        raise NotImplementedError()
