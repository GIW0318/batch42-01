import math


class ReflectionSetupError(Exception):
    pass


def _vec3(value, label):
    try:
        x, y, z = value
    except (TypeError, ValueError) as exc:
        raise ReflectionSetupError(
            "%s must be a 3-component vector, got %r" % (label, value)) from exc
    if not all(isinstance(component, (int, float)) and
               math.isfinite(component) for component in (x, y, z)):
        raise ReflectionSetupError(
            "%s contains a non-finite component: %r" % (label, value))
    return (float(x), float(y), float(z))


def _normalized(vector, label):
    length = math.sqrt(sum(component * component for component in vector))
    if length <= 0.0:
        raise ReflectionSetupError(
            "%s must have a non-zero length, got %r" % (label, vector))
    return tuple(component / length for component in vector)


def _reflection_matrix(point, normal):
    """Build the 4x4 mirror matrix M = I - 2 * n * n^T translated to the plane."""
    nx, ny, nz = normal
    px, py, pz = point
    d = nx * px + ny * py + nz * pz
    matrix = [[0.0] * 4 for _ in range(4)]
    for row in range(3):
        for col in range(3):
            matrix[row][col] = (
                1.0 if row == col else 0.0) \
                - 2.0 * normal[row] * normal[col]
        matrix[row][3] = 2.0 * d * normal[row]
    matrix[3][3] = 1.0
    return tuple(tuple(row) for row in matrix)


def _apply_matrix(matrix, point):
    x, y, z = point
    homogeneous = (x, y, z, 1.0)
    return tuple(
        sum(matrix[row][col] * homogeneous[col] for col in range(4))
        for row in range(3))


class PlanarReflection:
    def __init__(self, width=1920, height=1080, max_recursion=1):
        self._validate_resolution(width, height)
        self.width = int(width)
        self.height = int(height)
        self.max_recursion = min(int(max_recursion), 1)
        self._planes = {}
        self._targets = {}
        self._next_target = 1
        self._layers = None
        self._particles = True
        self._budget_ms = None
        self._wave_normals = {}

    @staticmethod
    def _validate_resolution(width, height):
        for label, value in (("width", width), ("height", height)):
            if not isinstance(value, (int, float)) or not math.isfinite(value) \
                    or value <= 0 or int(value) != value:
                raise ReflectionSetupError(
                    "%s must be a positive integer size, got %r"
                    % (label, value))

    def add_plane(self, plane_id, point, normal):
        plane_point = _vec3(point, "plane point")
        plane_normal = _normalized(
            _vec3(normal, "plane normal"), "plane normal")
        self._planes[plane_id] = {
            "point": plane_point,
            "normal": plane_normal,
            "matrix": _reflection_matrix(plane_point, plane_normal),
            "clip": (plane_normal[0], plane_normal[1], plane_normal[2],
                     -(plane_normal[0] * plane_point[0]
                       + plane_normal[1] * plane_point[1]
                       + plane_normal[2] * plane_point[2])),
        }
        self._wave_normals.setdefault(plane_id, (0.0, 0.0, 0.0))
        self._rebuild_target(plane_id)

    def reflect_point(self, plane_id, point):
        plane = self._require_plane(plane_id)
        world = _vec3(point, "point")
        return _apply_matrix(plane["matrix"], world)

    def clip_plane(self, plane_id, camera_position):
        """Return the oblique near clip plane (world-space ax+by+cz+d=0)."""
        plane = self._require_plane(plane_id)
        camera = _vec3(camera_position, "camera position")
        normal = plane["normal"]
        point = plane["point"]
        if normal[0] * camera[0] + normal[1] * camera[1] \
                + normal[2] * camera[2] \
                < normal[0] * point[0] + normal[1] * point[1] \
                + normal[2] * point[2]:
            normal = tuple(-component for component in normal)
        return (normal[0], normal[1], normal[2],
                -(normal[0] * point[0] + normal[1] * point[1]
                  + normal[2] * point[2]))

    def render(self, plane_id, camera, objects):
        plane = self._require_plane(plane_id)
        camera_data = self._normalize_camera(camera)
        normal = plane["normal"]
        point = plane["point"]
        camera_side = (
            normal[0] * camera_data["position"][0]
            + normal[1] * camera_data["position"][1]
            + normal[2] * camera_data["position"][2]
            - (normal[0] * point[0] + normal[1] * point[1]
               + normal[2] * point[2]))
        if camera_side <= 0.0:
            return {
                "enabled": False,
                "objects": [],
                "recursion": self.max_recursion,
                "static_culled": 0,
                "dynamic_culled": 0,
                "downsampled": False,
                "width": 0,
                "height": 0,
                "target": None,
            }

        target = self._ensure_target(plane_id)
        reflected_position = _apply_matrix(
            plane["matrix"], camera_data["position"])

        static_culled = 0
        dynamic_culled = 0
        visible = []
        for obj in objects:
            if self._layers is not None and \
                    not (int(obj.get("layer", 0)) & self._layers):
                continue
            obj_type = obj.get("type", "static")
            if obj_type == "particle" and not self._particles:
                continue
            position = self._object_position(obj)
            signed_distance = (
                normal[0] * position[0] + normal[1] * position[1]
                + normal[2] * position[2]
                - (normal[0] * point[0] + normal[1] * point[1]
                   + normal[2] * point[2]))
            if signed_distance < 0.0:
                if obj_type == "static":
                    static_culled += 1
                elif obj_type == "dynamic":
                    dynamic_culled += 1
                continue
            visible.append(obj["id"])

        downsampled = False
        width, height = self.width, self.height
        if self._budget_ms is not None:
            estimated_ms = 0.05 * len(objects) + 0.5
            if estimated_ms > self._budget_ms:
                downsampled = True
                scale = max(self._budget_ms / estimated_ms, 0.0)
                width = max(1, int(self.width * scale))
                height = max(1, int(self.height * scale))

        return {
            "enabled": True,
            "objects": visible,
            "recursion": self.max_recursion,
            "static_culled": static_culled,
            "dynamic_culled": dynamic_culled,
            "downsampled": downsampled,
            "width": width,
            "height": height,
            "target": target,
            "reflection_matrix": plane["matrix"],
            "reflected_camera": reflected_position,
            "clip_plane": self.clip_plane(
                plane_id, camera_data["position"]),
        }

    @staticmethod
    def _object_position(obj):
        position = obj.get("position")
        if position is not None:
            return _vec3(position, "object position")
        return _vec3((0.0, obj.get("y", 0.0), 0.0), "object position")

    @staticmethod
    def _normalize_camera(camera):
        if isinstance(camera, dict):
            position = camera.get("position", (0.0, 0.0, 0.0))
            near = camera.get("near", 0.1)
            far = camera.get("far", 1000.0)
            fov = camera.get("fov", 60.0)
            aspect = camera.get("aspect", 16.0 / 9.0)
        else:
            position = camera
            near, far, fov, aspect = 0.1, 1000.0, 60.0, 16.0 / 9.0
        normalized = {
            "position": _vec3(position, "camera position"),
            "near": near,
            "far": far,
            "fov": fov,
            "aspect": aspect,
        }
        for label, value in normalized.items():
            if label == "position":
                continue
            if not isinstance(value, (int, float)) or not math.isfinite(value):
                raise ReflectionSetupError(
                    "camera %s must be a finite number, got %r"
                    % (label, value))
        if normalized["near"] <= 0.0 or normalized["far"] <= normalized["near"] \
                or normalized["fov"] <= 0.0 or normalized["fov"] >= 180.0 \
                or normalized["aspect"] <= 0.0:
            raise ReflectionSetupError(
                "degenerate camera parameters: %r" % (normalized,))
        return normalized

    def set_resolution(self, width, height):
        self._validate_resolution(width, height)
        self.width = int(width)
        self.height = int(height)
        for plane_id in list(self._planes):
            self._rebuild_target(plane_id)

    def set_layers(self, mask):
        if not isinstance(mask, int) or isinstance(mask, bool) or mask < 0:
            raise ReflectionSetupError(
                "layer mask must be a non-negative integer, got %r"
                % (mask,))
        self._layers = mask

    def set_particles(self, enabled):
        self._particles = bool(enabled)

    def set_budget(self, ms):
        if ms is not None and (
                not isinstance(ms, (int, float)) or not math.isfinite(ms)
                or ms <= 0):
            raise ReflectionSetupError(
                "frame budget must be a positive number of ms, got %r" % (ms,))
        self._budget_ms = None if ms is None else float(ms)

    def set_wave_normal(self, plane_id, normal):
        self._require_plane(plane_id)
        wave = _vec3(normal, "wave normal")
        length = math.sqrt(
            wave[0] * wave[0] + wave[1] * wave[1] + wave[2] * wave[2])
        self._wave_normals[plane_id] = (
            (0.0, 0.0, 0.0) if length == 0.0
            else tuple(component / length for component in wave))

    def uv_offset(self, plane_id, uv):
        self._require_plane(plane_id)
        u, v = uv
        wave = self._wave_normals[plane_id]
        offset_u = 0.5 * wave[0]
        offset_v = 0.5 * wave[2]
        return ((u + offset_u) % 1.0, (v + offset_v) % 1.0)

    def reset_scene(self):
        self._planes.clear()
        self._targets.clear()
        self._wave_normals.clear()
        self._next_target = 1

    def targets(self):
        return dict(self._targets)

    def _require_plane(self, plane_id):
        try:
            return self._planes[plane_id]
        except KeyError:
            raise ReflectionSetupError(
                "unknown reflection plane %r" % (plane_id,)) from None

    def _ensure_target(self, plane_id):
        target = self._targets.get(plane_id)
        if target is None:
            target = {
                "id": self._next_target,
                "width": self.width,
                "height": self.height,
            }
            self._next_target += 1
            self._targets[plane_id] = target
        return target

    def _rebuild_target(self, plane_id):
        target = self._targets.get(plane_id)
        target_id = target["id"] if target else self._next_target
        if target is None:
            self._next_target += 1
        self._targets[plane_id] = {
            "id": target_id,
            "width": self.width,
            "height": self.height,
        }
