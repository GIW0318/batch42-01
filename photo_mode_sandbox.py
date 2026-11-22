class PhotoModeError(Exception):
    pass


_SCENE_KEYS = ("position", "weather", "picked", "cutscene", "time_ms",
               "progress", "scene_name")


class PhotoModeSandbox:
    def __init__(self):
        self._active = False
        self._scene = {
            "position": (0.0, 0.0, 0.0),
            "weather": "clear",
            "picked": [],
            "cutscene": None,
            "time_ms": 0,
            "progress": 0,
            "scene_name": "",
        }
        self._saved_scene = None
        self._frozen = False
        self._photos = []
        self._camera = {"x": 0.0, "y": 0.0, "z": 0.0, "fov": 60.0}
        self._colliders = []
        self._resolution = (1920, 1080)
        self._target_id = 1
        self._allocated = 0
        self._target_bytes = 0
        self._photo_buffer = 0
        self._health = 100
        self._feedback = None

    @staticmethod
    def _validate_resolution(width, height):
        if not isinstance(width, int) or not isinstance(height, int):
            raise PhotoModeError("resolution must be integers")
        if width <= 0 or height <= 0:
            raise PhotoModeError("resolution must be positive")

    @staticmethod
    def _validate_fov(fov):
        if not isinstance(fov, (int, float)) or not (0.0 < fov < 180.0):
            raise PhotoModeError("illegal field of view")

    def enter(self, scene_state):
        if self._active:
            raise PhotoModeError("photo mode already active")
        scene = {key: scene_state.get(key) for key in _SCENE_KEYS}
        scene["position"] = tuple(scene["position"] or (0.0, 0.0, 0.0))
        scene["picked"] = list(scene["picked"] or [])
        self._saved_scene = {
            "position": tuple(scene["position"]),
            "weather": scene["weather"],
            "picked": list(scene["picked"]),
            "cutscene": scene["cutscene"],
            "time_ms": scene["time_ms"],
            "progress": scene["progress"],
            "scene_name": scene["scene_name"],
        }
        self._scene = scene
        self._frozen = True
        self._active = True
        self._ensure_target()

    def exit(self):
        if not self._active:
            raise PhotoModeError("photo mode is not active")
        self._scene = {
            "position": tuple(self._saved_scene["position"]),
            "weather": self._saved_scene["weather"],
            "picked": list(self._saved_scene["picked"]),
            "cutscene": self._saved_scene["cutscene"],
            "time_ms": self._saved_scene["time_ms"],
            "progress": self._saved_scene["progress"],
            "scene_name": self._saved_scene["scene_name"],
        }
        self._saved_scene = None
        self._frozen = False
        self._active = False

    def _ensure_target(self):
        needed = self._resolution[0] * self._resolution[1] * 4
        if needed != self._target_bytes:
            self._allocated -= self._target_bytes
            self._target_bytes = needed
            self._allocated += self._target_bytes

    def take_photo(self, width=1920, height=1080, filter_name="none",
                   hide_ui=True):
        if not self._active:
            raise PhotoModeError("photo mode is not active")
        self._validate_resolution(width, height)
        needed = width * height * 4
        if needed > self._photo_buffer:
            self._allocated += needed - self._photo_buffer
            self._photo_buffer = needed
        photo = {
            "width": width,
            "height": height,
            "framing_aspect": self._resolution[0] / self._resolution[1],
            "layers": {
                "scene": {"filter": filter_name},
                "ui": {
                    "filter": "none",
                    "elements": [] if hide_ui else ["crosshair", "health"],
                },
            },
            "location": "%s@(%s,%s,%s)" % (
                self._scene["scene_name"], *self._scene["position"]),
            "scene": self._scene["scene_name"],
            "weather": self._scene["weather"],
            "time_ms": self._scene["time_ms"],
            "filter": filter_name,
        }
        self._photos.append(photo)
        return photo

    def set_camera(self, x, y, z, fov=60.0):
        self._validate_fov(fov)
        for (x0, y0, z0, x1, y1, z1) in self._colliders:
            if x0 <= x <= x1 and y0 <= y <= y1 and z0 <= z <= z1:
                raise PhotoModeError("camera inside loaded geometry")
        self._camera = {"x": x, "y": y, "z": z, "fov": fov}

    def add_collider(self, x0, y0, z0, x1, y1, z1):
        self._colliders.append((x0, y0, z0, x1, y1, z1))

    def set_resolution(self, width, height):
        self._validate_resolution(width, height)
        if (width, height) != self._resolution:
            self._resolution = (width, height)
            self._target_id += 1
            self._ensure_target()

    def set_weather(self, weather):
        self._scene["weather"] = weather

    def damage(self, amount):
        self._health = max(0, self._health - amount)
        self._feedback = {
            "type": "damage",
            "amount": amount,
            "health": self._health,
        }

    def health(self):
        return self._health

    def last_feedback(self):
        return self._feedback

    def world_tick(self, dt_ms):
        if not self._frozen:
            self._scene["time_ms"] += dt_ms

    def local_state(self):
        return {
            "position": tuple(self._scene["position"]),
            "weather": self._scene["weather"],
            "picked": list(self._scene["picked"]),
            "cutscene": self._scene["cutscene"],
            "time_ms": self._scene["time_ms"],
            "progress": self._scene["progress"],
            "scene_name": self._scene["scene_name"],
            "frozen": self._frozen,
            "target_id": self._target_id,
            "resolution": self._resolution,
        }

    def server_view(self):
        return {
            "frozen": False,
            "party_marker_visible": True,
            "position": tuple(self._scene["position"]),
            "health": self._health,
        }

    def photos(self):
        return list(self._photos)

    def allocated_bytes(self):
        return self._allocated

    def release(self):
        self._allocated = 0
        self._target_bytes = 0
        self._photo_buffer = 0

    def snapshot(self):
        return {
            "photos": [dict(photo) for photo in self._photos],
            "resolution": self._resolution,
            "health": self._health,
        }

    def restore(self, snapshot):
        self._photos = [dict(photo) for photo in snapshot.get("photos", [])]
        self._resolution = tuple(snapshot.get("resolution", (1920, 1080)))
        self._health = snapshot.get("health", 100)
