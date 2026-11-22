"""Input binding, combo window, deadzone and profile sync (skeleton).

One physical input drives one action per device, combos win over single keys,
device profiles restore by GUID and cloud sync is idempotent.  No behaviour
is implemented yet.
"""

BINDING_CONFLICT = "binding_conflict"
UNSUPPORTED_PROFILE_VERSION = "unsupported_profile_version"
UNKNOWN_DEVICE = "unknown_device"


class InputError(Exception):
    def __init__(self, code, message=""):
        self.code = code
        super().__init__(code if not message else "%s: %s" % (code, message))


class BindingConflict(InputError):
    pass


class InputRemapProfile:
    def __init__(self, combo_window=0.08, default_deadzone=0.15,
                 profile_version=2):
        self.combo_window = combo_window
        self.default_deadzone = default_deadzone
        self.profile_version = profile_version

    def bind(self, device_type, physical, action):
        raise NotImplementedError

    def bind_combo(self, device_type, physicals, action):
        raise NotImplementedError

    def unbind(self, device_type, physical):
        raise NotImplementedError

    def resolve(self, device_type, inputs):
        raise NotImplementedError

    def set_deadzone(self, device_type, value):
        raise NotImplementedError

    def deadzone(self, device_type):
        raise NotImplementedError

    def device_connected(self, guid, device_type, hardware_id=None):
        raise NotImplementedError

    def device_disconnected(self, guid):
        raise NotImplementedError

    def device_count(self):
        raise NotImplementedError

    def last_device(self):
        raise NotImplementedError

    def binding_count(self):
        raise NotImplementedError

    def set_game_override(self, game_id, bindings):
        raise NotImplementedError

    def clear_game_override(self, game_id):
        raise NotImplementedError

    def resolve_game(self, game_id, device_type, inputs):
        raise NotImplementedError

    def save_profile(self):
        raise NotImplementedError

    def load_profile(self, blob):
        raise NotImplementedError

    def sync_cloud(self, remote):
        raise NotImplementedError
