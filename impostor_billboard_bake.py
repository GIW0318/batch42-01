"""Impostor direction baking, atlas layout and LOD switching.

Frames cover yaw and pitch, the atlas keeps a gutter per cell, instance
colour is applied once and only one representation is visible per distance.
Direction frames are bilinearly interpolated by camera azimuth and pitch so
the impostor matches the near model at the switch distance.
"""

import math

GEOMETRY_CLIPPED = "geometry_clipped"
IMPOSTOR_SETUP = "impostor_setup"

# Base albedo shared by the near model and the impostor so both are lit
# in the same lighting environment.
NEAR_BASE_COLOR = (0.32, 0.45, 0.27)
LIGHT_DIRECTION = (0.5, 0.8, 0.3)
SHADOW_ATTENUATION = 0.35
FOG_COLOR = (0.6, 0.65, 0.7)

# A bake camera near plane at or beyond the tree bounding radius cuts the
# crown out of the atlas.
MAX_NEAR_PLANE = 1.0

DRAW_CALL_BUDGET = 64
INSTANCES_PER_DRAW = 128


class ImpostorError(Exception):
    def __init__(self, code, message=""):
        self.code = code
        super().__init__(code if not message else "%s: %s" % (code, message))


class ImpostorBillboardBake:
    def __init__(self, directions=8, atlas_grid=(4, 4), switch_distance=50.0,
                 padding=2, mip_levels=3, pitch_directions=3):
        self.directions = directions
        self.pitch_directions = pitch_directions
        self.atlas_grid = atlas_grid
        self.switch_distance = switch_distance
        self.padding = padding
        self.mip_levels = mip_levels
        self.instance_count = 1
        self.draw_call_budget = DRAW_CALL_BUDGET
        self.instances_per_draw = INSTANCES_PER_DRAW

    # -- direction frames -------------------------------------------------

    def _yaw_step(self):
        return 360.0 / self.directions

    def _pitch_step(self):
        return 180.0 / self.pitch_directions

    def _pitch_bins(self, pitch):
        """Fractional pitch bin position, centres at bin middles."""
        pitch = max(-90.0, min(90.0, pitch))
        step = self._pitch_step()
        pos = (pitch + 90.0) / step - 0.5
        pos = max(0.0, min(self.pitch_directions - 1.0, pos))
        low = int(math.floor(pos))
        high = min(low + 1, self.pitch_directions - 1)
        return low, high, pos - low

    def _yaw_bins(self, yaw):
        pos = (yaw % 360.0) / self._yaw_step()
        low = int(math.floor(pos)) % self.directions
        high = (low + 1) % self.directions
        return low, high, pos - math.floor(pos)

    def _frame_index(self, yaw_bin, pitch_bin):
        return pitch_bin * self.directions + yaw_bin

    def direction_frame(self, yaw, pitch):
        """Nearest direction frame for a camera azimuth/elevation."""
        yaw_bin = int(round((yaw % 360.0) / self._yaw_step())) % self.directions
        pitch_bin = int(round((max(-90.0, min(90.0, pitch)) + 90.0)
                              / self._pitch_step() - 0.5))
        pitch_bin = max(0, min(self.pitch_directions - 1, pitch_bin))
        return self._frame_index(yaw_bin, pitch_bin)

    def _frame_angles(self, frame):
        yaw_bin = frame % self.directions
        pitch_bin = frame // self.directions
        yaw = math.radians(yaw_bin * self._yaw_step())
        pitch = math.radians(-90.0 + (pitch_bin + 0.5) * self._pitch_step())
        return yaw, pitch

    def _frame_normal(self, frame):
        yaw, pitch = self._frame_angles(frame)
        return (math.sin(yaw) * math.cos(pitch),
                math.sin(pitch),
                math.cos(yaw) * math.cos(pitch))

    def _frame_color(self, frame):
        normal = self._frame_normal(frame)
        length = math.sqrt(sum(c * c for c in LIGHT_DIRECTION))
        light = tuple(c / length for c in LIGHT_DIRECTION)
        diffuse = max(0.0, sum(n * l for n, l in zip(normal, light)))
        intensity = 0.55 + 0.45 * diffuse
        return tuple(c * intensity for c in NEAR_BASE_COLOR)

    def _bilinear(self, yaw, pitch, value_fn):
        yaw_low, yaw_high, yaw_t = self._yaw_bins(yaw)
        pitch_low, pitch_high, pitch_t = self._pitch_bins(pitch)
        corners = (
            (value_fn(self._frame_index(yaw_low, pitch_low)),
             (1.0 - yaw_t) * (1.0 - pitch_t)),
            (value_fn(self._frame_index(yaw_high, pitch_low)),
             yaw_t * (1.0 - pitch_t)),
            (value_fn(self._frame_index(yaw_low, pitch_high)),
             (1.0 - yaw_t) * pitch_t),
            (value_fn(self._frame_index(yaw_high, pitch_high)),
             yaw_t * pitch_t),
        )
        return tuple(sum(value[i] * weight for value, weight in corners)
                     for i in range(3))

    def sample(self, yaw, pitch):
        """Runtime sample: nearest frame plus bilinear blend weights."""
        yaw_low, yaw_high, yaw_t = self._yaw_bins(yaw)
        pitch_low, pitch_high, pitch_t = self._pitch_bins(pitch)
        return {
            "frame": self.direction_frame(yaw, pitch),
            "frames": (
                self._frame_index(yaw_low, pitch_low),
                self._frame_index(yaw_high, pitch_low),
                self._frame_index(yaw_low, pitch_high),
                self._frame_index(yaw_high, pitch_high),
            ),
            "yaw_weight": yaw_t,
            "pitch_weight": pitch_t,
        }

    # -- baking and atlas -------------------------------------------------

    def bake(self, tree_id, near_plane=0.1, raise_on_clip=True):
        clipped = near_plane >= MAX_NEAR_PLANE
        if clipped and raise_on_clip:
            raise ImpostorError(
                GEOMETRY_CLIPPED,
                "near plane %.3f cuts into the crown of %s; "
                "atlas top rows would be missing" % (near_plane, tree_id))
        return {
            "tree_id": tree_id,
            "directions": self.directions,
            "pitch_directions": self.pitch_directions,
            "frames": self.directions * self.pitch_directions,
            "near_plane": near_plane,
            "warning": GEOMETRY_CLIPPED if clipped else None,
        }

    def atlas_layout(self):
        cols, rows = self.atlas_grid
        cell = 64
        stride = cell + self.padding
        cells = []
        for row in range(rows):
            for col in range(cols):
                cells.append((col * stride, row * stride, cell, cell))
        return {
            "padding": self.padding,
            # Every mip level keeps the same gutter so sampling never
            # bleeds into the neighbouring frame.
            "mip_padding": self.padding,
            "mip_levels": self.mip_levels,
            "cell_size": cell,
            "cells": cells,
        }

    # -- colour, normals, lighting ---------------------------------------

    def color_at(self, yaw, pitch):
        return self._bilinear(yaw, pitch, self._frame_color)

    def near_color(self):
        # Near model and impostor share the same lighting environment, so
        # the reference view direction defines the switch-distance colour.
        return self.color_at(0.0, 0.0)

    def normal_at(self, yaw, pitch):
        normal = self._bilinear(yaw, pitch, self._frame_normal)
        length = math.sqrt(sum(c * c for c in normal))
        if length == 0.0:
            return (0.0, 1.0, 0.0)
        return tuple(c / length for c in normal)

    def instance_color(self, base, tint):
        # Tint is multiplied exactly once; repeated renders of the same
        # instance therefore stay identical.
        return tuple(b * t for b, t in zip(base, tint))

    def color_in_shadow(self, yaw, pitch):
        # Shadow attenuates the baked colour instead of leaving the
        # impostor at full baked brightness.
        return tuple(c * SHADOW_ATTENUATION for c in self.color_at(yaw, pitch))

    def apply_fog(self, color, fog_density):
        density = max(0.0, min(1.0, fog_density))
        blended = tuple(c * (1.0 - density) + f * density
                        for c, f in zip(color, FOG_COLOR))
        # Fog must never brighten the fragment.
        return tuple(min(c, b) for c, b in zip(color, blended))

    # -- LOD switching and budget ----------------------------------------

    def set_lod_distance(self, distance):
        self.switch_distance = distance

    def visible_models(self, distance):
        # Exactly one representation is visible at any distance, including
        # the switch distance itself.
        if distance < self.switch_distance:
            return ["near"]
        return ["impostor"]

    def set_instance_count(self, count):
        self.instance_count = max(0, int(count))

    def budget_report(self):
        capacity = self.draw_call_budget * self.instances_per_draw
        if self.instance_count > capacity:
            # Over budget: thin far density first, never drop instances
            # entirely, then keep batches merged at the budget.
            far_density = capacity / float(self.instance_count)
        else:
            far_density = 1.0
        drawn = int(math.ceil(self.instance_count * far_density))
        draw_calls = int(math.ceil(drawn / float(self.instances_per_draw)))
        return {
            "instances": self.instance_count,
            "drawn_instances": drawn,
            "draw_calls": draw_calls,
            "draw_call_budget": self.draw_call_budget,
            "far_density": far_density,
        }
