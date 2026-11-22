class UnknownCombatantError(Exception):
    pass


class ThreatRegistry:
    def __init__(self, heal_multiplier=1.5, stealth_multiplier=0.5,
                 decay_per_second=0.0):
        self.heal_multiplier = heal_multiplier
        self.stealth_multiplier = stealth_multiplier
        self.decay_per_second = decay_per_second
        self._combatants = {}
        self._threat = {}
        self._applied = set()
        self._next_seq = 1
        self._pending = {}
        self._taunt = None
        self._vulnerable = True
        self._paused = False

    def add_combatant(self, combatant_id, role="dps", joined_at=0, owner=None):
        raise NotImplementedError()

    def submit_event(self, event):
        raise NotImplementedError()

    def flush(self):
        raise NotImplementedError()

    def threat(self, combatant_id):
        raise NotImplementedError()

    def effective_threat(self, combatant_id):
        raise NotImplementedError()

    def current_target(self, now_ms=0):
        raise NotImplementedError()

    def taunt(self, combatant_id, until_ms):
        raise NotImplementedError()

    def set_vulnerable(self, vulnerable):
        raise NotImplementedError()

    def set_stealth(self, combatant_id, stealth):
        raise NotImplementedError()

    def spawn_add(self, parent_id, add_id):
        raise NotImplementedError()

    def tick(self, dt_ms):
        raise NotImplementedError()

    def set_paused(self, paused):
        raise NotImplementedError()

    def change_scene(self):
        raise NotImplementedError()

    def wipe(self):
        raise NotImplementedError()

    def snapshot(self):
        raise NotImplementedError()

    def restore(self, snapshot):
        raise NotImplementedError()
