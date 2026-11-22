class PhotoModeError(Exception):
    pass


class PhotoModeSandbox:
    def __init__(self):
        self._active = False
        self._scene = {}
        self._photos = []
        self._camera = {"x": 0.0, "y": 0.0, "z": 0.0, "fov": 60.0}
        self._colliders = []
        self._resolution = (1920, 1080)
        self._target_id = 1
        self._allocated = 0
        self._health = 100
        self._feedback = None
        self._weather = "clear"

    def enter(self, scene_state):
        raise NotImplementedError()

    def exit(self):
        raise NotImplementedError()

    def take_photo(self, width=1920, height=1080, filter_name="none",
                   hide_ui=True):
        raise NotImplementedError()

    def set_camera(self, x, y, z, fov=60.0):
        raise NotImplementedError()

    def add_collider(self, x0, y0, z0, x1, y1, z1):
        raise NotImplementedError()

    def set_resolution(self, width, height):
        raise NotImplementedError()

    def set_weather(self, weather):
        raise NotImplementedError()

    def damage(self, amount):
        raise NotImplementedError()

    def health(self):
        raise NotImplementedError()

    def last_feedback(self):
        raise NotImplementedError()

    def world_tick(self, dt_ms):
        raise NotImplementedError()

    def local_state(self):
        raise NotImplementedError()

    def server_view(self):
        raise NotImplementedError()

    def photos(self):
        raise NotImplementedError()

    def allocated_bytes(self):
        raise NotImplementedError()

    def release(self):
        raise NotImplementedError()

    def snapshot(self):
        raise NotImplementedError()

    def restore(self, snapshot):
        raise NotImplementedError()
