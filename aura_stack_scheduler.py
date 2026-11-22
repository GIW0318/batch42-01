class AuraStateError(Exception):
    pass


class AuraScheduler:
    def __init__(self, max_auras=32):
        self.max_auras = max_auras
        self._definitions = {}
        self._instances = {}
        self._next_instance = 1
        self._applied_events = {}
        self._paused = False
        self._clock_ms = 0
        self._immune = set()
        self._haste = {}

    def define_aura(self, aura_id, name, kind="buff", max_stacks=1,
                    duration_ms=1000, stack_mode="refresh", tick_ms=None,
                    stack_bonus=0):
        raise NotImplementedError()

    def apply(self, event_id, aura_id, target_id, source_id="self", now_ms=0):
        raise NotImplementedError()

    def remove_instance(self, instance_id, now_ms=0):
        raise NotImplementedError()

    def dispel(self, target_id, category, count=1):
        raise NotImplementedError()

    def kill(self, target_id):
        raise NotImplementedError()

    def revive(self, target_id):
        raise NotImplementedError()

    def set_paused(self, paused):
        raise NotImplementedError()

    def set_immune(self, target_id, immune):
        raise NotImplementedError()

    def set_haste(self, target_id, multiplier):
        raise NotImplementedError()

    def travel(self, target_id):
        raise NotImplementedError()

    def tick(self, now_ms):
        raise NotImplementedError()

    def stacks(self, target_id, aura_id):
        raise NotImplementedError()

    def remaining_ms(self, instance_id, now_ms):
        raise NotImplementedError()

    def stat_bonus(self, target_id):
        raise NotImplementedError()

    def instances(self, target_id):
        raise NotImplementedError()

    def snapshot(self):
        raise NotImplementedError()

    def restore(self, snapshot):
        raise NotImplementedError()
