import math


class OutlineSetupError(Exception):
    pass


def _positive_float(value, field):
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise OutlineSetupError(f"{field} must be a positive number") from None
    if not math.isfinite(number) or number <= 0.0:
        raise OutlineSetupError(f"{field} must be a positive number")
    return number


def _normalized(values):
    if isinstance(values, dict):
        values = values.get("normal")
    try:
        vector = tuple(float(value) for value in values)
    except (TypeError, ValueError):
        return None
    if len(vector) != 3 or not all(math.isfinite(value) for value in vector):
        return None
    length = math.sqrt(sum(value * value for value in vector))
    if length <= 1e-12:
        return None
    return tuple(value / length for value in vector)


class ToonOutline:
    _DEFAULT_PASS_COST_MS = 1.0
    _INTERNAL_EDGE_COS = math.cos(math.radians(30.0))
    _SILHOUETTE_DEPTH_RATIO = 0.1

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

    @property
    def width_px(self):
        return self._width_px

    @width_px.setter
    def width_px(self, value):
        self._width_px = _positive_float(value, "width_px")

    @property
    def base_height(self):
        return self._base_height

    @base_height.setter
    def base_height(self, value):
        self._base_height = _positive_float(value, "base_height")

    @property
    def resolution(self):
        return self._resolution

    @resolution.setter
    def resolution(self, value):
        self._resolution = self._validate_resolution(value)

    def set_resolution(self, width, height=None):
        if height is None and not isinstance(width, (str, bytes)):
            try:
                width, height = width
            except (TypeError, ValueError):
                pass
        self._resolution = self._validate_resolution((width, height))
        return self._resolution

    @staticmethod
    def _validate_resolution(resolution):
        try:
            width, height = resolution
        except (TypeError, ValueError):
            raise OutlineSetupError(
                "resolution must contain width and height") from None
        width = _positive_float(width, "resolution width")
        height = _positive_float(height, "resolution height")
        return (width, height)

    def effective_width(self):
        return self.width_px * (self.resolution[1] / self.base_height)

    def compute_width(self, distance):
        return self.effective_width()

    def classify_edge(self, normal_a, normal_b, depth_a, depth_b,
                      hard_edge=False):
        try:
            depth_a = float(depth_a)
            depth_b = float(depth_b)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(depth_a) or not math.isfinite(depth_b):
            return None

        depth_scale = max(abs(depth_a), abs(depth_b), 1e-12)
        if abs(depth_a - depth_b) / depth_scale > self._SILHOUETTE_DEPTH_RATIO:
            return "silhouette"

        if hard_edge:
            return "silhouette"

        normal_a = _normalized(normal_a)
        normal_b = _normalized(normal_b)
        if normal_a is None or normal_b is None:
            return None
        dot = sum(a * b for a, b in zip(normal_a, normal_b))
        if dot < self._INTERNAL_EDGE_COS:
            return "internal"
        return None

    def set_normals(self, normals):
        if normals is None:
            raise OutlineSetupError("normals are required")
        result = []
        for item in normals:
            if isinstance(item, dict):
                source = dict(item)
                vector = source.get("normal")
            else:
                source = {}
                vector = item
            normalized = _normalized(vector)
            if normalized is None:
                raise OutlineSetupError("invalid normal")
            source["normal"] = normalized
            source["hard_edge"] = bool(source.get("hard_edge", False))
            result.append(source)
        self._normals = result

    def smooth_normals(self):
        source = [dict(item) for item in self._normals]
        smoothed = []
        for index, item in enumerate(source):
            if item["hard_edge"]:
                smoothed.append(dict(item))
                continue

            candidates = [item["normal"]]
            for neighbor_index in (index - 1, index + 1):
                if 0 <= neighbor_index < len(source):
                    neighbor = source[neighbor_index]
                    if not neighbor["hard_edge"]:
                        candidates.append(neighbor["normal"])

            total = tuple(
                sum(vector[axis] for vector in candidates)
                for axis in range(3)
            )
            normal = _normalized(total) or item["normal"]
            result = dict(item)
            result["normal"] = normal
            smoothed.append(result)

        self._normals = [dict(item) for item in smoothed]
        return smoothed

    def set_normal_map(self, data):
        if data is None:
            raise OutlineSetupError("normal map is required")
        if isinstance(data, (str, bytes)):
            raise OutlineSetupError("normal map is required")
        try:
            if len(data) == 0:
                raise OutlineSetupError("normal map is required")
        except TypeError:
            raise OutlineSetupError("normal map is required") from None
        self._normal_map = data

    def is_occluded(self, outline_depth, wall_depth):
        if outline_depth is None or wall_depth is None:
            return False
        try:
            return float(outline_depth) > float(wall_depth)
        except (TypeError, ValueError):
            return False

    def vr_params(self):
        width = self.effective_width()
        return {
            "left": {"width": width, "offset": 0.0},
            "right": {"width": width, "offset": 0.0},
        }

    def apply_postprocess(self, mask):
        self._mask = [] if mask is None else list(mask)
        return list(self._mask)

    def coverage(self):
        return list(self._mask)

    def set_budget(self, budget_ms):
        try:
            budget = float(budget_ms)
        except (TypeError, ValueError):
            raise OutlineSetupError("budget_ms must be a number") from None
        if not math.isfinite(budget):
            raise OutlineSetupError("budget_ms must be finite")
        budget = max(0.0, budget)
        self._budget_ms = budget
        self._quality = min(1.0, budget / self._DEFAULT_PASS_COST_MS)

    def quality(self):
        return self._quality

    def render_order(self):
        return ["opaque", "outline", "transparent"]
