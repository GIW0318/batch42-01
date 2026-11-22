"""Game audio bus, ducking priority and configuration restore (skeleton).

Ducking takes the strictest active duck by priority, mute and solo never
change the stored bus gain, configuration reloads before playback and the
mix frame drops low-priority sources over budget.  No behaviour implemented.
"""

BUS_NOT_FOUND = "bus_not_found"
SOURCE_NOT_FOUND = "source_not_found"
CONFIG_INVALID = "config_invalid"


class AudioError(Exception):
    def __init__(self, code, message=""):
        self.code = code
        super().__init__(code if not message else "%s: %s" % (code, message))


class AudioDuckingMixer:
    def __init__(self, buses=("master", "music", "sfx", "voice"),
                 silence_floor_db=-60.0, budget_sources=32):
        self.buses = list(buses)
        self.silence_floor_db = silence_floor_db
        self.budget_sources = budget_sources

    def set_bus_gain(self, bus, db):
        raise NotImplementedError

    def bus_gain(self, bus):
        raise NotImplementedError

    def trigger_duck(self, source_id, bus, gain_db, priority=0, at=None):
        raise NotImplementedError

    def release_duck(self, source_id):
        raise NotImplementedError

    def release_all(self):
        raise NotImplementedError

    def mute(self, bus):
        raise NotImplementedError

    def unmute(self, bus):
        raise NotImplementedError

    def solo(self, bus):
        raise NotImplementedError

    def unsolo(self):
        raise NotImplementedError

    def is_muted(self, bus):
        raise NotImplementedError

    def audible(self, bus):
        raise NotImplementedError

    def effective_gain(self, bus):
        raise NotImplementedError

    def set_automation(self, bus, db):
        raise NotImplementedError

    def save_config(self):
        raise NotImplementedError

    def load_config(self, config):
        raise NotImplementedError

    def rebuild_channels(self, device_id, layout):
        raise NotImplementedError

    def channels(self):
        raise NotImplementedError

    def mix_frame(self, sources):
        raise NotImplementedError
