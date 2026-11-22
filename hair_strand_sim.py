"""Hair strand integration, collision, LOD and reset (skeleton).

Simulation advances with a fixed substep and capped catch-up, collisions are
swept at low frame rates, teleporting resets history and saved poses reload
without a first-frame fly-in.  No behaviour is implemented yet.
"""

HAIR_SETUP = "hair_setup"
COLLISION_FAILED = "collision_failed"


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

    def reset(self, position=(0.0, 0.0, 0.0)):
        raise NotImplementedError

    def step(self, dt, fps=None):
        raise NotImplementedError

    def simulate(self, seconds, fps=60):
        raise NotImplementedError

    def strand_positions(self):
        raise NotImplementedError

    def set_collider(self, head_center, head_radius, shoulder_y=None):
        raise NotImplementedError

    def set_lod(self, level):
        raise NotImplementedError

    def teleport(self, position):
        raise NotImplementedError

    def save_appearance(self):
        raise NotImplementedError

    def load_appearance(self, snapshot):
        raise NotImplementedError

    def budget_report(self):
        raise NotImplementedError

    def set_wind(self, wind_id, vector):
        raise NotImplementedError

    def wind_vector(self):
        raise NotImplementedError

    def set_hairstyle(self, name):
        raise NotImplementedError

    def collision_cache_size(self):
        raise NotImplementedError

    def set_enabled(self, enabled):
        raise NotImplementedError

    def set_character_count(self, count):
        raise NotImplementedError

    def move_head(self, delta):
        raise NotImplementedError
