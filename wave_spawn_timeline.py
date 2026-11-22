class WaveConfigError(Exception):
    pass


class WaveTimeline:
    def __init__(self, season_id, frame_budget=8):
        self.season_id = season_id
        self.frame_budget = frame_budget
        self._waves = []
        self._clock_ms = 0
        self._paused = False
        self._speed = 1.0
        self._spawned = []
        self._next_wave_index = 0
        self._pending = []
        self._claimed = set()

    def load_waves(self, waves):
        raise NotImplementedError()

    def trigger_wave(self, wave_id):
        raise NotImplementedError()

    def update(self, dt_ms):
        raise NotImplementedError()

    def now_ms(self):
        raise NotImplementedError()

    def set_paused(self, paused):
        raise NotImplementedError()

    def set_speed(self, speed):
        raise NotImplementedError()

    def stats(self):
        raise NotImplementedError()

    def kill(self, monster_id):
        raise NotImplementedError()

    def leak(self, monster_id):
        raise NotImplementedError()

    def claim_reward(self, wave_id):
        raise NotImplementedError()

    def snapshot(self):
        raise NotImplementedError()

    def restore(self, snapshot):
        raise NotImplementedError()
