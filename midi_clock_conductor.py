class MidiDeviceError(Exception):
    pass


class MidiConductor:
    def __init__(self, tempo_bpm=120.0, ppq=480):
        self.tempo_bpm = tempo_bpm
        self.ppq = ppq
        self._ports = {}
        self._events = []
        self._tempo_changes = [(0.0, tempo_bpm)]
        self._playing = False
        self._paused = False
        self._now_ms = 0.0
        self._loop_base_ms = 0.0
        self._external_clock = False
        self._last_external_ms = 0.0
        self._note_pairs = {}

    def add_port(self, name):
        raise NotImplementedError()

    def remove_port(self, name):
        raise NotImplementedError()

    def set_external_clock(self, enabled, jitter_ms=0.0):
        raise NotImplementedError()

    def external_tick(self, timestamp_ms):
        raise NotImplementedError()

    def schedule_note(self, port, note, velocity, beat, duration_beats):
        raise NotImplementedError()

    def set_tempo(self, bpm, at_ms):
        raise NotImplementedError()

    def play(self):
        raise NotImplementedError()

    def pause(self):
        raise NotImplementedError()

    def stop(self):
        raise NotImplementedError()

    def close(self):
        raise NotImplementedError()

    def advance(self, target_ms):
        raise NotImplementedError()

    def active_notes(self, port):
        raise NotImplementedError()

    def now_ms(self):
        raise NotImplementedError()

    def loop(self):
        raise NotImplementedError()

    def quantize(self, tick, grid):
        raise NotImplementedError()
