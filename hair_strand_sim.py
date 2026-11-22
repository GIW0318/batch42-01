"""Hair strand integration, collision, LOD and reset.

Simulation advances with a fixed substep and capped catch-up, collisions are
swept at low frame rates, teleporting resets history and saved poses reload
without a first-frame fly-in.
"""

import math

HAIR_SETUP = "hair_setup"
COLLISION_FAILED = "collision_failed"

GRAVITY = (0.0, -9.8, 0.0)
POINTS_PER_STRAND = 6
SEGMENT_LENGTH = 0.25
CONSTRAINT_ITERATIONS = 3
DAMPING = 0.98
COST_PER_STRAND_MS = 0.01
GOLDEN_ANGLE = 2.399963229728653
DEFAULT_HAIRSTYLE = "default"


class HairError(Exception):
    def __init__(self, code, message=""):
        self.code = code
        super().__init__(code if not message else "%s: %s" % (code, message))


class HairStrandSim:
    def __init__(self, max_substeps=4, substep=1.0 / 120.0, budget_ms=2.0,
                 strands=32):
        self.max_substeps = max_substeps
        self.substep = substep
        self.budget_ms = budget_ms
        self.strands = strands
        self.character_count = 1
        self.enabled = True
        self.lod = 0
        self.hairstyle = DEFAULT_HAIRSTYLE
        self.head_center = (0.0, 0.0, 0.0)
        self.head_radius = 1.0
        self.shoulder_y = None
        self._winds = {}
        self._accumulator = 0.0
        self._collision_cache = set()
        self._substeps_last_frame = 0
        self._requested_strands = strands
        self._simulated_strands = strands
        self._points = []
        self._previous = []
        self._build_rest_pose()

    def reset(self, position=(0.0, 0.0, 0.0)):
        self.head_center = tuple(position)
        self._accumulator = 0.0
        self._collision_cache.clear()
        self._substeps_last_frame = 0
        self._build_rest_pose()
        self._update_budget()

    def step(self, dt, fps=None):
        if not self.enabled:
            self._substeps_last_frame = 0
            self._update_budget()
            return
        self._accumulator += dt
        self._collision_cache.clear()
        substeps = 0
        while self._accumulator >= self.substep and substeps < self.max_substeps:
            self._substep()
            self._accumulator -= self.substep
            substeps += 1
        if substeps >= self.max_substeps:
            # Drop the backlog instead of bursting dozens of catch-up steps
            # after a hitch.
            self._accumulator = 0.0
        self._substeps_last_frame = substeps
        self._update_budget()
        self._repair_non_finite()

    def simulate(self, seconds, fps=60):
        frames = int(round(seconds * fps))
        dt = 1.0 / fps
        for _ in range(frames):
            self.step(dt)

    def strand_positions(self):
        return [[(point[0], point[1], point[2]) for point in strand]
                for strand in self._points]

    def set_collider(self, head_center, head_radius, shoulder_y=None):
        self.head_center = tuple(head_center)
        self.head_radius = float(head_radius)
        self.shoulder_y = shoulder_y

    def set_lod(self, level):
        self.lod = max(0, int(level))

    def teleport(self, position):
        self.head_center = tuple(position)
        self._accumulator = 0.0
        self._collision_cache.clear()
        self._build_rest_pose()

    def save_appearance(self):
        return {
            "hairstyle": self.hairstyle,
            "head_center": tuple(self.head_center),
            "pose": [[tuple(point) for point in strand]
                     for strand in self._points],
        }

    def load_appearance(self, snapshot):
        if not isinstance(snapshot, dict) or "pose" not in snapshot:
            raise HairError(HAIR_SETUP, "appearance snapshot is missing a pose")
        self.hairstyle = snapshot.get("hairstyle") or DEFAULT_HAIRSTYLE
        self.head_center = tuple(snapshot.get("head_center", (0.0, 0.0, 0.0)))
        self._points = [[list(point) for point in strand]
                        for strand in snapshot["pose"]]
        # History starts at the loaded pose so the first frame does not
        # fly in from the origin or reuse the previous hairstyle's pose.
        self._previous = [[point[:] for point in strand]
                          for strand in self._points]
        self._accumulator = 0.0
        self._collision_cache.clear()

    def budget_report(self):
        return {
            "budget_ms": self.budget_ms,
            "substeps_last_frame": self._substeps_last_frame,
            "requested_strands": self._requested_strands,
            "simulated_strands": self._simulated_strands,
            "main_character_degraded":
                self._simulated_strands < self.strands,
        }

    def set_wind(self, wind_id, vector):
        self._winds[wind_id] = tuple(vector)

    def wind_vector(self):
        total = [0.0, 0.0, 0.0]
        for vector in self._winds.values():
            total[0] += vector[0]
            total[1] += vector[1]
            total[2] += vector[2]
        return (total[0], total[1], total[2])

    def set_hairstyle(self, name):
        self.hairstyle = name
        self._accumulator = 0.0
        self._collision_cache.clear()
        self._build_rest_pose()

    def collision_cache_size(self):
        return len(self._collision_cache)

    def set_enabled(self, enabled):
        self.enabled = bool(enabled)

    def set_character_count(self, count):
        self.character_count = max(1, int(count))
        self._update_budget()

    def move_head(self, delta):
        dx, dy, dz = delta
        cx, cy, cz = self.head_center
        self.head_center = (cx + dx, cy + dy, cz + dz)
        for strand, previous in zip(self._points, self._previous):
            for k in range(len(strand)):
                strand[k][0] += dx
                strand[k][1] += dy
                strand[k][2] += dz
                previous[k][0] += dx
                previous[k][1] += dy
                previous[k][2] += dz

    def _strand_direction(self, index):
        elevation = (math.pi / 2.0) * (index + 0.5) / self.strands
        theta = index * GOLDEN_ANGLE
        cos_elevation = math.cos(elevation)
        return (math.cos(theta) * cos_elevation,
                math.sin(elevation),
                math.sin(theta) * cos_elevation)

    def _build_rest_pose(self):
        cx, cy, cz = self.head_center
        self._points = []
        for index in range(self.strands):
            dx, dy, dz = self._strand_direction(index)
            strand = []
            for k in range(POINTS_PER_STRAND):
                radius = self.head_radius + k * SEGMENT_LENGTH
                strand.append([cx + dx * radius,
                               cy + dy * radius,
                               cz + dz * radius])
            self._points.append(strand)
        self._previous = [[point[:] for point in strand]
                          for strand in self._points]

    def _substep(self):
        wind = self.wind_vector()
        ax = GRAVITY[0] + wind[0]
        ay = GRAVITY[1] + wind[1]
        az = GRAVITY[2] + wind[2]
        dt2 = self.substep * self.substep
        damping = DAMPING
        rest = SEGMENT_LENGTH
        cx, cy, cz = self.head_center
        head_radius = self.head_radius
        shoulder_y = self.shoulder_y
        cache = self._collision_cache
        iterations = max(1, CONSTRAINT_ITERATIONS - self.lod)

        for strand_index, (strand, previous) in enumerate(
                zip(self._points, self._previous)):
            for k in range(1, len(strand)):
                point = strand[k]
                prev = previous[k]
                nx = point[0] + (point[0] - prev[0]) * damping + ax * dt2
                ny = point[1] + (point[1] - prev[1]) * damping + ay * dt2
                nz = point[2] + (point[2] - prev[2]) * damping + az * dt2
                prev[0], prev[1], prev[2] = point[0], point[1], point[2]
                point[0], point[1], point[2] = nx, ny, nz
            for _ in range(iterations):
                for k in range(1, len(strand)):
                    p1 = strand[k - 1]
                    p2 = strand[k]
                    dx = p2[0] - p1[0]
                    dy = p2[1] - p1[1]
                    dz = p2[2] - p1[2]
                    dist = math.sqrt(dx * dx + dy * dy + dz * dz)
                    if dist < 1e-12:
                        continue
                    diff = (dist - rest) / dist
                    if k == 1:
                        p2[0] -= dx * diff
                        p2[1] -= dy * diff
                        p2[2] -= dz * diff
                    else:
                        half = 0.5 * diff
                        p1[0] += dx * half
                        p1[1] += dy * half
                        p1[2] += dz * half
                        p2[0] -= dx * half
                        p2[1] -= dy * half
                        p2[2] -= dz * half
            # Collision runs last so a substep never ends inside the head
            # or below the shoulders, even at low frame rates and low LOD.
            for k in range(1, len(strand)):
                point = strand[k]
                dx = point[0] - cx
                dy = point[1] - cy
                dz = point[2] - cz
                dist = math.sqrt(dx * dx + dy * dy + dz * dz)
                if dist < head_radius:
                    if dist < 1e-9:
                        dx, dy, dz = 0.0, 1.0, 0.0
                        dist = 1.0
                    scale = head_radius / dist
                    point[0] = cx + dx * scale
                    point[1] = cy + dy * scale
                    point[2] = cz + dz * scale
                    cache.add((strand_index, k))
                if shoulder_y is not None and point[1] < shoulder_y:
                    point[1] = shoulder_y
                    cache.add((strand_index, k))

    def _repair_non_finite(self):
        for strand in self._points:
            for point in strand:
                for value in point:
                    if not math.isfinite(value):
                        self._build_rest_pose()
                        return

    def _update_budget(self):
        requested = self.strands * self.character_count
        affordable = int(self.budget_ms / COST_PER_STRAND_MS)
        # The main character's strands are never degraded; only far
        # characters are shed when the frame budget is exceeded.
        allowed = max(self.strands, affordable)
        self._requested_strands = requested
        self._simulated_strands = min(requested, allowed)
