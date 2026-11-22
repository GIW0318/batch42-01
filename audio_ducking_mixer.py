"""Game audio bus, ducking priority and configuration restore.

Bus state (the gain stored by the user and the mute flag) is kept separate
from transient mixing state (automation, ducks and solo).  This keeps a saved
snapshot stable even while a fade or a side-chain duck is active.
"""

import math
import time


BUS_NOT_FOUND = "bus_not_found"
SOURCE_NOT_FOUND = "source_not_found"
CONFIG_INVALID = "config_invalid"


_CHANNEL_LAYOUTS = {
    "mono": ("mono",),
    "stereo": ("left", "right"),
    "quad": ("front_left", "front_right", "rear_left", "rear_right"),
    "5.0": (
        "left",
        "right",
        "center",
        "left_surround",
        "right_surround",
    ),
    "5.1": (
        "left",
        "right",
        "center",
        "lfe",
        "left_surround",
        "right_surround",
    ),
    "7.1": (
        "left",
        "right",
        "center",
        "lfe",
        "left_surround",
        "right_surround",
        "left_back",
        "right_back",
    ),
}

_LAYOUT_ALIASES = {
    "1": "mono",
    "1.0": "mono",
    "2": "stereo",
    "2.0": "stereo",
    "stereo2.0": "stereo",
    "4": "quad",
    "4.0": "quad",
    "5": "5.0",
    "6": "5.1",
    "8": "7.1",
}


class AudioError(Exception):
    def __init__(self, code, message=""):
        self.code = code
        super().__init__(code if not message else "%s: %s" % (code, message))


class AudioDuckingMixer:
    def __init__(self, buses=("master", "music", "sfx", "voice"),
                 silence_floor_db=-60.0, budget_sources=32):
        self.buses = list(buses)
        self.silence_floor_db = self._finite_number(
            silence_floor_db, "silence_floor_db")
        self.budget_sources = self._non_negative_int(
            budget_sources, "budget_sources")

        self._gains = {bus: 0.0 for bus in self.buses}
        self._muted = {bus: False for bus in self.buses}
        self._automation = {bus: 0.0 for bus in self.buses}
        self._ducks = {}
        self._trigger_order = 0
        self._soloed = set()

        self._channels = {
            "device_id": None,
            "layout": None,
            "channels": [],
            "mapping": {},
            "index_to_channel": {},
        }
        self.ready = True
        self.last_mix_report = None

    @staticmethod
    def _finite_number(value, name):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise AudioError(CONFIG_INVALID, "%s must be a number" % name)
        value = float(value)
        if not math.isfinite(value):
            raise AudioError(CONFIG_INVALID, "%s must be finite" % name)
        return value

    @staticmethod
    def _non_negative_int(value, name):
        if isinstance(value, bool) or not isinstance(value, int):
            raise AudioError(CONFIG_INVALID, "%s must be an integer" % name)
        if value < 0:
            raise AudioError(CONFIG_INVALID, "%s must not be negative" % name)
        return value

    def _require_bus(self, bus):
        if bus not in self._gains:
            raise AudioError(BUS_NOT_FOUND, str(bus))
        return bus

    def set_bus_gain(self, bus, db):
        self._require_bus(bus)
        self._gains[bus] = self._finite_number(db, "gain_db")

    def bus_gain(self, bus):
        self._require_bus(bus)
        return self._gains[bus]

    def trigger_duck(self, source_id, bus, gain_db, priority=0, at=None):
        self._require_bus(bus)
        gain_db = self._finite_number(gain_db, "gain_db")
        priority = self._finite_number(priority, "priority")
        if at is None:
            at = time.monotonic()
        else:
            at = self._finite_number(at, "at")

        self._trigger_order += 1
        self._ducks[source_id] = {
            "source_id": source_id,
            "bus": bus,
            "gain_db": gain_db,
            "priority": priority,
            "at": at,
            "order": self._trigger_order,
        }

    def release_duck(self, source_id):
        if source_id not in self._ducks:
            raise AudioError(SOURCE_NOT_FOUND, str(source_id))
        del self._ducks[source_id]

    def release_all(self):
        self._ducks.clear()

    def mute(self, bus):
        self._require_bus(bus)
        self._muted[bus] = True

    def unmute(self, bus):
        self._require_bus(bus)
        self._muted[bus] = False

    def solo(self, bus):
        self._require_bus(bus)
        self._soloed.add(bus)

    def unsolo(self):
        self._soloed.clear()

    def is_muted(self, bus):
        self._require_bus(bus)
        return self._muted[bus]

    def audible(self, bus):
        self._require_bus(bus)
        if self._muted[bus]:
            return False
        return not self._soloed or bus in self._soloed

    def _selected_duck(self, bus):
        candidates = [
            duck for duck in self._ducks.values() if duck["bus"] == bus
        ]
        if not candidates:
            return None
        return max(
            candidates,
            key=lambda duck: (
                duck["priority"],
                duck["at"],
                duck["order"],
            ),
        )

    def effective_gain(self, bus):
        self._require_bus(bus)
        gain = self._gains[bus] + self._automation[bus]
        duck = self._selected_duck(bus)
        if duck is not None:
            gain += duck["gain_db"]
        return max(self.silence_floor_db, gain)

    def set_automation(self, bus, db):
        self._require_bus(bus)
        self._automation[bus] = self._finite_number(db, "automation_db")

    def save_config(self):
        return {
            "version": 1,
            "buses": {
                bus: {
                    "gain": self._gains[bus],
                    "muted": self._muted[bus],
                }
                for bus in self.buses
            },
        }

    def load_config(self, config):
        self.ready = False
        if not isinstance(config, dict):
            raise AudioError(CONFIG_INVALID, "config must be an object")

        version = config.get("version", 1)
        if version != 1:
            raise AudioError(CONFIG_INVALID, "unsupported config version")

        buses = config.get("buses")
        if not isinstance(buses, dict):
            raise AudioError(CONFIG_INVALID, "config buses must be an object")

        staged_gains = dict(self._gains)
        staged_muted = dict(self._muted)
        for bus, state in buses.items():
            if bus not in staged_gains:
                raise AudioError(CONFIG_INVALID, "unknown bus %s" % bus)
            if not isinstance(state, dict):
                raise AudioError(
                    CONFIG_INVALID, "bus state for %s must be an object" % bus)
            if "gain" not in state:
                raise AudioError(
                    CONFIG_INVALID, "bus state for %s is missing gain" % bus)
            staged_gains[bus] = self._finite_number(
                state["gain"], "gain for %s" % bus)
            if "muted" in state:
                if not isinstance(state["muted"], bool):
                    raise AudioError(
                        CONFIG_INVALID,
                        "muted state for %s must be boolean" % bus,
                    )
                staged_muted[bus] = state["muted"]
            else:
                staged_muted[bus] = False

        self._gains = staged_gains
        self._muted = staged_muted
        self.ready = True
        return {"ready": True}

    def rebuild_channels(self, device_id, layout):
        if not isinstance(layout, str) or not layout.strip():
            raise AudioError(CONFIG_INVALID, "layout must be a string")
        key = layout.strip().lower()
        key = _LAYOUT_ALIASES.get(key, key)
        if key not in _CHANNEL_LAYOUTS:
            raise AudioError(CONFIG_INVALID, "unknown layout %s" % layout)

        channels = list(_CHANNEL_LAYOUTS[key])
        mapping = {name: index for index, name in enumerate(channels)}
        index_to_channel = {
            index: name for index, name in enumerate(channels)
        }
        self._channels = {
            "device_id": device_id,
            "layout": layout,
            "channels": channels,
            "mapping": mapping,
            "index_to_channel": index_to_channel,
        }
        return self.channels()

    def channels(self):
        channels = self._channels
        return {
            "device_id": channels["device_id"],
            "layout": channels["layout"],
            "channels": list(channels["channels"]),
            "mapping": dict(channels["mapping"]),
            "index_to_channel": dict(channels["index_to_channel"]),
        }

    def mix_frame(self, sources):
        if not self.ready:
            raise AudioError(CONFIG_INVALID, "audio configuration is not ready")
        if sources is None:
            sources = ()
        try:
            source_list = list(sources)
        except TypeError:
            raise AudioError(CONFIG_INVALID, "sources must be iterable")

        ranked = []
        for index, source in enumerate(source_list):
            if not isinstance(source, dict):
                raise AudioError(
                    CONFIG_INVALID, "source at index %d must be an object" % index)
            priority = source.get("priority", 0)
            priority = self._finite_number(
                priority, "priority for source at index %d" % index)
            ranked.append((priority, index, source))

        ranked.sort(key=lambda item: (-item[0], item[1]))
        selected = ranked[:self.budget_sources]
        dropped = ranked[self.budget_sources:]

        mixed_sources = [dict(source) for _, _, source in selected]
        dropped_sources = []
        for _, _, source in dropped:
            report = dict(source)
            report["reason"] = "budget_exceeded"
            dropped_sources.append(report)

        result = {
            "mixed": len(mixed_sources),
            "dropped": len(dropped_sources),
            "mixed_sources": mixed_sources,
            "dropped_sources": dropped_sources,
            "budget_sources": self.budget_sources,
        }
        self.last_mix_report = result
        return result
