"""Decal ordering, atlas sampling and host lifecycle.

Decals sort by layer and submission order, the atlas clamps sampling to one
cell, the limit fades old decals instead of dropping them and host changes
move their decals.
"""

import math
import numbers


DECAL_LIMIT = "decal_limit"
UNKNOWN_DECAL = "unknown_decal"

_UV_EPSILON = 1.0e-6
_ATLAS_CELL_RESOLUTION = 256.0
_FADE_STEP = 0.25


class DecalError(Exception):
    def __init__(self, code, message=""):
        self.code = code
        super().__init__(code if not message else "%s: %s" % (code, message))


def _vector3(value, name):
    if len(value) != 3:
        raise ValueError("%s must contain three values" % name)
    vector = tuple(float(component) for component in value)
    if not all(math.isfinite(component) for component in vector):
        raise ValueError("%s must contain finite values" % name)
    return vector


def _normalized(value, name):
    vector = _vector3(value, name)
    length = math.sqrt(sum(component * component for component in vector))
    if length == 0.0:
        raise ValueError("%s must be non-zero" % name)
    return tuple(component / length for component in vector)


def _cross(left, right):
    return (
        left[1] * right[2] - left[2] * right[1],
        left[2] * right[0] - left[0] * right[2],
        left[0] * right[1] - left[1] * right[0],
    )


def _dot(left, right):
    return sum(a * b for a, b in zip(left, right))


def _clamp(value, low, high):
    return max(low, min(high, value))


class DecalProjectionStack:
    def __init__(self, limit=32, atlas_grid=(4, 4), padding=1):
        self.limit = self._validated_limit(limit)
        self.atlas_grid = self._validated_grid(atlas_grid)
        if not isinstance(padding, numbers.Real) or not math.isfinite(
                float(padding)) or padding < 0:
            raise ValueError("padding must be a finite non-negative number")
        self.padding = float(padding)
        self._decals = {}
        self._host_positions = {}
        self._next_submission = 0

    @staticmethod
    def _validated_limit(limit):
        if isinstance(limit, bool) or not isinstance(limit, numbers.Integral):
            raise DecalError(DECAL_LIMIT, "limit must be an integer")
        if limit < 0:
            raise DecalError(DECAL_LIMIT, "limit must be non-negative")
        return int(limit)

    @staticmethod
    def _validated_grid(grid):
        if len(grid) != 2:
            raise ValueError("atlas_grid must contain two values")
        columns, rows = grid
        if (isinstance(columns, bool) or isinstance(rows, bool)
                or not isinstance(columns, numbers.Integral)
                or not isinstance(rows, numbers.Integral)
                or columns <= 0 or rows <= 0):
            raise ValueError("atlas_grid dimensions must be positive integers")
        return int(columns), int(rows)

    def _validate_cell(self, cell):
        if len(cell) != 2:
            raise ValueError("atlas cell must contain two values")
        column, row = cell
        if (isinstance(column, bool) or isinstance(row, bool)
                or not isinstance(column, numbers.Integral)
                or not isinstance(row, numbers.Integral)):
            raise ValueError("atlas cell coordinates must be integers")
        columns, rows = self.atlas_grid
        if not (0 <= column < columns and 0 <= row < rows):
            raise ValueError("atlas cell is outside the atlas grid")
        return int(column), int(row)

    def _snapshot(self, entry, duplicate=False):
        state = entry["state"]
        return {
            "decal_id": entry["decal_id"],
            "host_id": entry["host_id"],
            "position": entry["position"],
            "normal": entry["normal"],
            "layer": entry["layer"],
            "atlas_cell": entry["atlas_cell"],
            "transparent": entry["transparent"],
            "system_id": entry["system_id"],
            "order": entry["submission"],
            "creation_seq": entry["submission"],
            "submission_seq": entry["submission"],
            "order_key": (entry["layer"], entry["submission"]),
            "state": state,
            "alpha": entry["alpha"],
            "depth_tested": state in ("active", "fading") and entry["alpha"] > 0,
            "backface_culling": False,
            "batch_key": (
                entry["system_id"],
                entry["atlas_cell"],
                entry["layer"],
                entry["transparent"],
            ),
            "duplicate": duplicate,
        }

    def _sorted_entries(self):
        return sorted(
            self._decals.values(),
            key=lambda entry: (entry["layer"], entry["submission"]),
        )

    @staticmethod
    def _active(entry):
        return entry["state"] == "active" and entry["alpha"] > 0.0

    def _drop_entry(self, decal_id):
        entry = self._decals.pop(decal_id, None)
        if entry is None:
            return
        host_id = entry["host_id"]
        if not any(item["host_id"] == host_id
                   for item in self._decals.values()):
            self._host_positions.pop(host_id, None)

    def _tick_fades(self):
        for decal_id, entry in list(self._decals.items()):
            if entry["state"] != "fading":
                continue
            entry["alpha"] = max(0.0, entry["alpha"] - _FADE_STEP)
            if entry["alpha"] <= _UV_EPSILON:
                entry["alpha"] = 0.0
                entry["state"] = "faded"
                self._drop_entry(decal_id)

    def _enforce_limit(self, restore=False):
        ordered = self._sorted_entries()
        active = [entry for entry in ordered if self._active(entry)]
        excess = max(0, len(active) - self.limit)

        if excess:
            for index, entry in enumerate(active[:excess]):
                entry["state"] = "fading"
                entry["alpha"] = max(
                    _FADE_STEP,
                    1.0 - _FADE_STEP * (index + 1),
                )

        if restore and len(active) < self.limit:
            available = self.limit - len(active)
            fading = [
                entry for entry in reversed(ordered)
                if entry["state"] == "fading" and entry["alpha"] > 0.0
            ]
            for entry in fading[:available]:
                entry["state"] = "active"
                entry["alpha"] = 1.0

    def add_decal(self, host_id, position, normal, layer=0,
                  atlas_cell=(0, 0), transparent=False, system_id="default",
                  decal_id=None):
        position = _vector3(position, "position")
        normal = _normalized(normal, "normal")
        cell = self._validate_cell(atlas_cell)

        if not isinstance(layer, numbers.Real) or not math.isfinite(float(layer)):
            raise ValueError("layer must be a finite number")
        layer = float(layer)

        submission = self._next_submission
        if decal_id is None:
            while "decal-%d" % submission in self._decals:
                submission += 1
            decal_id = "decal-%d" % submission
        if decal_id == "":
            raise ValueError("decal_id must not be empty")

        existing = self._decals.get(decal_id)
        if existing is not None:
            return self._snapshot(existing, duplicate=True)

        self._tick_fades()

        host_id = host_id if host_id is not None else "__world__"
        if host_id not in self._host_positions:
            self._host_positions[host_id] = position

        self._next_submission = submission + 1
        entry = {
            "decal_id": decal_id,
            "host_id": host_id,
            "position": position,
            "normal": normal,
            "layer": layer,
            "atlas_cell": cell,
            "transparent": bool(transparent),
            "system_id": system_id,
            "submission": submission,
            "state": "active",
            "alpha": 1.0,
        }
        self._decals[decal_id] = entry
        self._enforce_limit()
        return self._snapshot(entry)

    def decal_order(self):
        return [entry["decal_id"] for entry in self._sorted_entries()]

    def visible_decals(self):
        return [
            entry["decal_id"]
            for entry in self._sorted_entries()
            if entry["state"] in ("active", "fading") and entry["alpha"] > 0.0
        ]

    def remove_decal(self, decal_id):
        if decal_id not in self._decals:
            return False
        self._drop_entry(decal_id)
        return True

    def sample_atlas(self, cell, u, v, mip=0):
        cell = self._validate_cell(cell)
        if (isinstance(mip, bool) or not isinstance(mip, numbers.Integral)
                or mip < 0):
            raise ValueError("mip must be a non-negative integer")

        u = float(u)
        v = float(v)
        if not math.isfinite(u):
            u = 0.0
        if not math.isfinite(v):
            v = 0.0

        columns, rows = self.atlas_grid
        cell_width = 1.0 / columns
        cell_height = 1.0 / rows
        mip_scale = float(2 ** int(mip))
        padding_u = (self.padding * mip_scale) / _ATLAS_CELL_RESOLUTION
        padding_v = (self.padding * mip_scale) / _ATLAS_CELL_RESOLUTION
        inset_u = min(0.5 - _UV_EPSILON, max(_UV_EPSILON, padding_u))
        inset_v = min(0.5 - _UV_EPSILON, max(_UV_EPSILON, padding_v))

        local_u = min(max(u, 0.0), 1.0)
        local_v = min(max(v, 0.0), 1.0)
        clamped = not (
            inset_u <= local_u <= 1.0 - inset_u
            and inset_v <= local_v <= 1.0 - inset_v
        )
        safe_local_u = _clamp(local_u, inset_u, 1.0 - inset_u)
        safe_local_v = _clamp(local_v, inset_v, 1.0 - inset_v)

        column, row = cell
        atlas_u = column * cell_width + safe_local_u * cell_width
        atlas_v = row * cell_height + safe_local_v * cell_height
        return {
            "cell": cell,
            "u": atlas_u,
            "v": atlas_v,
            "atlas_u": atlas_u,
            "atlas_v": atlas_v,
            "local_u": safe_local_u,
            "local_v": safe_local_v,
            "mip": int(mip),
            "clamped": clamped,
            "padding_u": inset_u * cell_width,
            "padding_v": inset_v * cell_height,
        }

    def atlas_cell_of_sample(self, cell, u, v):
        cell = self._validate_cell(cell)
        sample = self.sample_atlas(cell, u, v)
        return sample["cell"]

    def set_limit(self, limit):
        self.limit = self._validated_limit(limit)
        self._enforce_limit(restore=True)
        return {
            "limit": self.limit,
            "fading": [
                entry["decal_id"] for entry in self._sorted_entries()
                if entry["state"] == "fading"
            ],
        }

    def fade_state(self, decal_id):
        entry = self._decals.get(decal_id)
        if entry is None:
            raise DecalError(UNKNOWN_DECAL, str(decal_id))
        return {
            "decal_id": decal_id,
            "state": entry["state"],
            "alpha": entry["alpha"],
            "depth_tested": entry["state"] in ("active", "fading"),
        }

    def clear_scene(self):
        count = len(self._decals)
        self._decals.clear()
        self._host_positions.clear()
        self._next_submission = 0
        return count

    def host_destroyed(self, host_id):
        removed = [
            entry["decal_id"] for entry in self._decals.values()
            if entry["host_id"] == host_id
        ]
        for decal_id in removed:
            self._drop_entry(decal_id)
        self._host_positions.pop(host_id, None)
        return removed

    def update_host(self, host_id, position, normal):
        if host_id not in self._host_positions:
            return []

        position = _vector3(position, "position")
        normal = _normalized(normal, "normal")
        previous = self._host_positions[host_id]
        delta = tuple(
            position[index] - previous[index] for index in range(3)
        )
        affected = []
        for entry in self._decals.values():
            if entry["host_id"] != host_id:
                continue
            entry["position"] = tuple(
                entry["position"][index] + delta[index] for index in range(3)
            )
            entry["normal"] = normal
            affected.append(entry["decal_id"])
        self._host_positions[host_id] = position
        return affected

    def decal_transform(self, decal_id):
        entry = self._decals.get(decal_id)
        if entry is None:
            raise DecalError(UNKNOWN_DECAL, str(decal_id))

        normal = entry["normal"]
        reference = (0.0, 1.0, 0.0)
        if abs(_dot(normal, reference)) > 0.999:
            reference = (1.0, 0.0, 0.0)
        tangent = _cross(reference, normal)
        tangent_length = math.sqrt(_dot(tangent, tangent))
        tangent = tuple(component / tangent_length for component in tangent)
        bitangent = _cross(normal, tangent)
        return {
            "position": entry["position"],
            "normal": normal,
            "tangent": tangent,
            "bitangent": bitangent,
            "layer": entry["layer"],
            "atlas_cell": entry["atlas_cell"],
            "host_id": entry["host_id"],
            "backface_culling": False,
            "depth_tested": entry["state"] in ("active", "fading"),
            "depth_offset": _UV_EPSILON,
        }

    def atlas_cell_size(self):
        columns, rows = self.atlas_grid
        return (1.0 / columns, 1.0 / rows)

    def visible_from(self, camera_position):
        _vector3(camera_position, "camera_position")
        return self.visible_decals()

    def depth_tested(self, decal_id):
        entry = self._decals.get(decal_id)
        return (
            entry is not None
            and entry["state"] in ("active", "fading")
            and entry["alpha"] > 0.0
        )
