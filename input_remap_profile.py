"""Input binding, combo window, deadzone and profile sync.

The profile keeps one binding table per device type and one snapshot per
physical controller identity.  A controller is identified by its hardware id
when one is available (so Bluetooth and wired transports share a profile),
otherwise by its GUID.  Reconnecting a known controller restores its snapshot;
an unknown controller is never silently given another controller's bindings.
"""

from __future__ import annotations

import copy
import json
import math
from collections import defaultdict


BINDING_CONFLICT = "binding_conflict"
UNSUPPORTED_PROFILE_VERSION = "unsupported_profile_version"
UNKNOWN_DEVICE = "unknown_device"


class InputError(Exception):
    def __init__(self, code, message=""):
        self.code = code
        super().__init__(code if not message else "%s: %s" % (code, message))


class BindingConflict(InputError):
    pass


def _canonical_physical(value):
    if value is None:
        raise InputError("invalid_physical", "physical input is required")
    if isinstance(value, str):
        value = value.strip()
        if not value:
            raise InputError("invalid_physical", "physical input is required")
        return value.upper()
    return str(value)


def _canonical_id(value):
    if value is None:
        raise InputError("invalid_device", "device id is required")
    return str(value).strip()


def _number(value, field, default=None):
    if value is None:
        if default is None:
            raise InputError("invalid_profile", "%s is required" % field)
        return default
    if isinstance(value, bool):
        raise InputError("invalid_profile", "%s must be a number" % field)
    try:
        result = float(value)
    except (TypeError, ValueError):
        raise InputError("invalid_profile", "%s must be a number" % field)
    if not math.isfinite(result):
        raise InputError("invalid_profile", "%s must be finite" % field)
    return result


class _Profile:
    __slots__ = ("bindings", "combos", "deadzone")

    def __init__(self):
        self.bindings = {}
        # frozenset(physicals) -> (sorted tuple of physicals, action)
        self.combos = {}
        self.deadzone = None

    def clone(self):
        result = _Profile()
        result.bindings = dict(self.bindings)
        result.combos = dict(self.combos)
        result.deadzone = self.deadzone
        return result

    def add_binding(self, physical, action):
        physical = _canonical_physical(physical)
        if action is None:
            self.bindings.pop(physical, None)
            return
        old = self.bindings.get(physical)
        if old is not None and old != action:
            raise BindingConflict(
                BINDING_CONFLICT,
                "%s is already bound to %s" % (physical, old),
            )
        self.bindings[physical] = action

    def add_combo(self, physicals, action):
        members = frozenset(_canonical_physical(item) for item in physicals)
        if len(members) < 2:
            raise InputError("invalid_combo", "a combo needs at least two inputs")
        old = self.combos.get(members)
        if old is not None and old[1] != action:
            raise BindingConflict(
                BINDING_CONFLICT,
                "combo %s is already bound to %s"
                % ("+".join(sorted(members)), old[1]),
            )
        self.combos[members] = (tuple(sorted(members)), action)

    def remove_binding(self, physical):
        self.bindings.pop(_canonical_physical(physical), None)

    def remove_combo(self, physicals):
        members = frozenset(_canonical_physical(item) for item in physicals)
        self.combos.pop(members, None)

    def to_dict(self):
        return {
            "bindings": dict(self.bindings),
            "combos": [
                {"physicals": list(members), "action": action}
                for members, action in self.combos.values()
            ],
            "deadzone": self.deadzone,
        }

    @classmethod
    def from_dict(cls, data):
        if not isinstance(data, dict):
            raise InputError("invalid_profile", "profile entry must be an object")

        result = cls()
        bindings = data.get("bindings", {})
        if bindings is None:
            bindings = {}
        if not isinstance(bindings, dict):
            raise InputError("invalid_profile", "bindings must be an object")
        for physical, action in bindings.items():
            if action is None:
                result.bindings[_canonical_physical(physical)] = None
            else:
                result.add_binding(physical, action)

        combos = data.get("combos", [])
        if combos is None:
            combos = []
        if not isinstance(combos, list):
            raise InputError("invalid_profile", "combos must be a list")
        for combo in combos:
            if isinstance(combo, dict):
                physicals = combo.get("physicals")
                action = combo.get("action")
            elif isinstance(combo, (list, tuple)) and len(combo) == 2:
                physicals, action = combo
            else:
                raise InputError("invalid_profile", "invalid combo entry")
            if not isinstance(physicals, (list, tuple, set, frozenset)):
                raise InputError("invalid_profile", "combo physicals must be a list")
            result.add_combo(physicals, action)

        deadzone = data.get("deadzone")
        if deadzone is not None:
            deadzone = _number(deadzone, "deadzone")
            if deadzone < 0.0 or deadzone > 1.0:
                raise InputError(
                    "invalid_deadzone", "deadzone must be between 0 and 1"
                )
        result.deadzone = deadzone
        return result


class _Event:
    __slots__ = ("physical", "time", "value", "arrival", "index")

    def __init__(self, physical, time, value, arrival, index):
        self.physical = physical
        self.time = time
        self.value = value
        self.arrival = arrival
        self.index = index


class InputRemapProfile:
    def __init__(self, combo_window=0.08, default_deadzone=0.15,
                 profile_version=2):
        self.combo_window = _number(
            combo_window, "combo_window", default=0.08
        )
        if self.combo_window < 0:
            raise InputError("invalid_combo_window", "combo window cannot be negative")
        self.default_deadzone = _number(
            default_deadzone, "default_deadzone", default=0.15
        )
        if self.default_deadzone < 0.0 or self.default_deadzone > 1.0:
            raise InputError(
                "invalid_deadzone", "deadzone must be between 0 and 1"
            )
        self.profile_version = profile_version

        self._type_profiles = {}
        # identity -> {"device_type": ..., "profiles": {device_type: _Profile}}
        self._identities = {}
        self._guid_to_identity = {}
        self._connected_guids = set()
        self._active_identities = {}
        self._seen_identity_types = set()
        self._game_overrides = {}
        self._last_device = None

    # ------------------------------------------------------------------
    # Binding and resolution
    # ------------------------------------------------------------------

    def bind(self, device_type, physical, action):
        device_type = _canonical_id(device_type)
        profile = self._writable_profile(device_type)
        profile.add_binding(physical, action)
        return self

    def bind_combo(self, device_type, physicals, action):
        device_type = _canonical_id(device_type)
        if isinstance(physicals, str) or not hasattr(physicals, "__iter__"):
            raise InputError("invalid_combo", "combo physicals must be iterable")
        profile = self._writable_profile(device_type)
        if action is None:
            profile.remove_combo(physicals)
            return self
        profile.add_combo(physicals, action)
        return self

    def unbind(self, device_type, physical):
        device_type = _canonical_id(device_type)
        profile = self._writable_profile(device_type)
        if isinstance(physical, (list, tuple, set, frozenset)):
            items = list(physical)
            if len(items) >= 2:
                profile.remove_combo(items)
            elif len(items) == 1:
                profile.remove_binding(items[0])
        else:
            profile.remove_binding(physical)
        return self

    def resolve(self, device_type, inputs):
        device_type = _canonical_id(device_type)
        profile = self._profile(device_type)
        resolved = self._resolve_profile(profile, inputs)
        if resolved:
            self._last_device = device_type
        return resolved

    def _resolve_profile(self, profile, inputs):
        events = self._parse_events(inputs, profile)
        if not events:
            return []

        events.sort(key=lambda event: (event.time, event.index))
        available = set(range(len(events)))
        combo_hits = []

        while True:
            best = None
            for order, (members, (_, action)) in enumerate(profile.combos.items()):
                match = self._find_combo_match(
                    events, available, members, self.combo_window
                )
                if match is None:
                    continue
                trigger_index, start_index, span = match
                trigger_time = events[trigger_index].time
                # A longer combo wins when two combo definitions overlap;
                # otherwise the earliest completion wins.
                key = (-len(members), trigger_time, span, order)
                if best is None or key < best[0]:
                    best = (key, members, action, match)
            if best is None:
                break

            _, members, action, match = best
            trigger_index, start_index, _ = match
            trigger_time = events[trigger_index].time
            combo_hits.append((trigger_time, 0, trigger_index, action))

            start_time = events[start_index].time
            horizon = trigger_time + self.combo_window
            consumed = set()
            for index in available:
                event = events[index]
                if (event.physical in members
                        and start_time <= event.time <= horizon):
                    consumed.add(index)
            # Consume at least the matched window, including duplicate events
            # that arrive between its first and last physical input.
            for index in range(start_index, trigger_index + 1):
                if index in available and events[index].physical in members:
                    consumed.add(index)
            available.difference_update(consumed)

        single_hits = []
        seen = set()
        for index in sorted(available, key=lambda item: (
                events[item].time, events[item].index)):
            event = events[index]
            action = profile.bindings.get(event.physical)
            if action is None:
                continue
            key = (event.physical, round(event.time, 9))
            if key in seen:
                continue
            seen.add(key)
            single_hits.append((event.time, 1, event.index, action))

        hits = combo_hits + single_hits
        hits.sort(key=lambda item: (item[0], item[1], item[2]))
        return [item[3] for item in hits]

    def _find_combo_match(self, events, available, members, window):
        if len(members) < 2:
            return None
        relevant = [
            index for index in sorted(available)
            if events[index].physical in members
        ]
        best = None
        for start_pos, start_index in enumerate(relevant):
            seen = set()
            for end_pos in range(start_pos, len(relevant)):
                end_index = relevant[end_pos]
                seen.add(events[end_index].physical)
                if len(seen) != len(members):
                    continue
                span = events[end_index].time - events[start_index].time
                if span > window + 1e-12:
                    break
                key = (
                    events[end_index].time,
                    span,
                    start_pos,
                    end_pos,
                )
                candidate = (key, end_index, start_index, span)
                if best is None or key < best[0]:
                    best = candidate
                break
        if best is None:
            return None
        return best[1], best[2], best[3]

    def _parse_events(self, inputs, profile):
        if inputs is None:
            return []
        if isinstance(inputs, str):
            inputs = [inputs]
        elif isinstance(inputs, dict):
            inputs = [inputs]
        events = []
        deadzone = profile.deadzone
        if deadzone is None:
            deadzone = self.default_deadzone
        for index, item in enumerate(inputs):
            event = self._parse_event(item, index)
            if event is None:
                continue
            if event.value is not None and abs(event.value) < deadzone:
                continue
            events.append(event)
        return events

    def _parse_event(self, item, index):
        physical = None
        time = 0.0
        value = None
        arrival = None

        if isinstance(item, dict):
            physical = item.get(
                "physical",
                item.get("input", item.get("key", item.get("button"))),
            )
            time = item.get(
                "event_time",
                item.get("time", item.get("timestamp", item.get("t", 0.0))),
            )
            value = item.get("value", item.get("axis", item.get("amount")))
            arrival = item.get(
                "arrival_time",
                item.get("arrival", item.get("received_at", time)),
            )
        elif isinstance(item, (list, tuple)):
            if not item:
                return None
            physical = item[0]
            if len(item) > 1:
                time = item[1]
            if len(item) > 2:
                value = item[2]
            if len(item) > 3:
                arrival = item[3]
        elif isinstance(item, str):
            physical = item
        elif hasattr(item, "physical"):
            physical = getattr(item, "physical", None)
            time = getattr(
                item, "event_time",
                getattr(item, "time", getattr(item, "timestamp", 0.0)),
            )
            value = getattr(item, "value", getattr(item, "axis", None))
            arrival = getattr(item, "arrival_time", time)
        else:
            return None

        if physical is None:
            return None
        try:
            physical = _canonical_physical(physical)
        except InputError:
            return None
        try:
            time = float(time)
        except (TypeError, ValueError):
            time = 0.0
        if not math.isfinite(time):
            time = 0.0
        if value is not None:
            try:
                value = float(value)
            except (TypeError, ValueError):
                value = None
            if value is not None and not math.isfinite(value):
                value = None
        if arrival is None:
            arrival = time
        try:
            arrival = float(arrival)
        except (TypeError, ValueError):
            arrival = time
        return _Event(physical, time, value, arrival, index)

    # ------------------------------------------------------------------
    # Deadzone
    # ------------------------------------------------------------------

    def set_deadzone(self, device_type, value):
        device_type = _canonical_id(device_type)
        value = _number(value, "deadzone")
        if value < 0.0 or value > 1.0:
            raise InputError(
                "invalid_deadzone", "deadzone must be between 0 and 1"
            )
        self._writable_profile(device_type).deadzone = value
        return self

    def deadzone(self, device_type):
        device_type = _canonical_id(device_type)
        profile = self._profile(device_type)
        if profile.deadzone is None:
            return self.default_deadzone
        return profile.deadzone

    # ------------------------------------------------------------------
    # Hotplug and controller identity
    # ------------------------------------------------------------------

    def device_connected(self, guid, device_type, hardware_id=None):
        guid = _canonical_id(guid)
        device_type = _canonical_id(device_type)
        if hardware_id is not None:
            identity = "hw:%s" % _canonical_id(hardware_id)
        else:
            identity = "guid:%s" % guid

        status = "connected"
        if guid in self._guid_to_identity:
            identity = self._guid_to_identity[guid]
        elif identity in self._identities:
            self._guid_to_identity[guid] = identity
        else:
            profile = _Profile()
            if device_type not in self._seen_identity_types:
                # The first controller of a type adopts settings made before it
                # was connected.  Later, different GUIDs get an empty profile
                # and an explicit rebind request.
                type_profile = self._type_profiles.get(device_type)
                if type_profile is not None:
                    profile = type_profile.clone()
            else:
                status = "needs_rebind"
            self._identities[identity] = {
                "device_type": device_type,
                "profiles": {device_type: profile},
            }
            self._guid_to_identity[guid] = identity
            self._seen_identity_types.add(device_type)

        record = self._identities[identity]
        if device_type not in record["profiles"]:
            record["profiles"][device_type] = _Profile()

        self._connected_guids.add(guid)
        self._active_identities[device_type] = identity
        return {
            "status": status,
            "guid": guid,
            "device_type": device_type,
            "identity": identity,
            "restored": status == "connected",
        }

    def device_disconnected(self, guid):
        guid = _canonical_id(guid)
        identity = self._guid_to_identity.get(guid)
        if identity is None or guid not in self._connected_guids:
            raise InputError(UNKNOWN_DEVICE, "unknown or disconnected device")

        self._connected_guids.discard(guid)
        # Keep the last profile active while the cable is out.  Reconnecting
        # the same controller restores it, and connecting an unknown GUID
        # switches to a fresh profile (with a rebind prompt) instead of
        # silently borrowing this one.
        return {"status": "disconnected", "guid": guid, "identity": identity}

    def device_count(self):
        identities = {
            self._guid_to_identity[guid]
            for guid in self._connected_guids
            if guid in self._guid_to_identity
        }
        return len(identities)

    def last_device(self):
        return self._last_device

    # ------------------------------------------------------------------
    # Game-specific overrides
    # ------------------------------------------------------------------

    def set_game_override(self, game_id, bindings):
        game_id = _canonical_id(game_id)
        override = self._parse_override(bindings)
        self._game_overrides[game_id] = override
        return self

    def clear_game_override(self, game_id):
        game_id = _canonical_id(game_id)
        self._game_overrides.pop(game_id, None)
        return self

    def resolve_game(self, game_id, device_type, inputs):
        game_id = _canonical_id(game_id)
        device_type = _canonical_id(device_type)
        override = self._game_overrides.get(game_id)
        if override is None:
            return self.resolve(device_type, inputs)

        profile = self._profile(device_type).clone()
        overridden_physicals = set(override.bindings)
        if overridden_physicals:
            profile.combos = {
                members: value
                for members, value in profile.combos.items()
                if not (members & overridden_physicals)
            }
        profile.bindings.update(override.bindings)
        for members, value in override.combos.items():
            profile.combos[members] = value
        if override.deadzone is not None:
            profile.deadzone = override.deadzone

        resolved = self._resolve_profile(profile, inputs)
        if resolved:
            self._last_device = device_type
        return resolved

    def _parse_override(self, bindings):
        override = _Profile()
        if bindings is None:
            return override
        if not isinstance(bindings, dict):
            raise InputError(
                "invalid_override", "game override must be an object"
            )

        structured = (
            isinstance(bindings.get("bindings"), dict)
            or isinstance(bindings.get("combos"), list)
            or "deadzone" in bindings
        )
        if structured:
            if "bindings" in bindings:
                raw_bindings = bindings["bindings"]
                if not isinstance(raw_bindings, dict):
                    raise InputError(
                        "invalid_override", "bindings must be an object"
                    )
                for physical, action in raw_bindings.items():
                    if action is None:
                        override.bindings[_canonical_physical(physical)] = None
                    else:
                        override.add_binding(physical, action)
            for combo in bindings.get("combos", []):
                if isinstance(combo, dict):
                    physicals = combo.get("physicals")
                    action = combo.get("action")
                elif isinstance(combo, (list, tuple)) and len(combo) == 2:
                    physicals, action = combo
                else:
                    raise InputError("invalid_override", "invalid combo entry")
                override.add_combo(physicals, action)
            if bindings.get("deadzone") is not None:
                value = _number(bindings["deadzone"], "deadzone")
                if value < 0.0 or value > 1.0:
                    raise InputError(
                        "invalid_deadzone", "deadzone must be between 0 and 1"
                    )
                override.deadzone = value
        else:
            for physical, action in bindings.items():
                if action is None:
                    override.bindings[_canonical_physical(physical)] = None
                else:
                    override.add_binding(physical, action)
        return override

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def save_profile(self):
        return {
            "profile_version": self.profile_version,
            "combo_window": self.combo_window,
            "default_deadzone": self.default_deadzone,
            "type_profiles": {
                device_type: profile.to_dict()
                for device_type, profile in self._type_profiles.items()
            },
            "identities": {
                identity: {
                    "device_type": record["device_type"],
                    "profiles": {
                        device_type: profile.to_dict()
                        for device_type, profile
                        in record["profiles"].items()
                    },
                }
                for identity, record in self._identities.items()
            },
            "guid_map": dict(self._guid_to_identity),
            "seen_identity_types": sorted(self._seen_identity_types),
            "last_identities": dict(self._active_identities),
            "game_overrides": {
                game_id: override.to_dict()
                for game_id, override in self._game_overrides.items()
            },
        }

    def load_profile(self, blob):
        parsed = self._parse_blob(blob)
        connected_guids = set(self._connected_guids)
        active_identities = dict(self._active_identities)

        self.combo_window = parsed["combo_window"]
        self.default_deadzone = parsed["default_deadzone"]
        self._type_profiles = parsed["type_profiles"]
        self._identities = parsed["identities"]
        self._guid_to_identity = parsed["guid_map"]
        self._seen_identity_types = parsed["seen_identity_types"]
        self._game_overrides = parsed["game_overrides"]

        # Importing a profile does not unplug controllers that are still
        # present.  Keep only runtime links that also exist in the imported
        # profile; the new configuration takes effect immediately.
        self._connected_guids = {
            guid for guid in connected_guids
            if guid in self._guid_to_identity
        }
        self._active_identities = {}
        for device_type, identity in active_identities.items():
            record = self._identities.get(identity)
            if record is not None and device_type in record["profiles"]:
                self._active_identities[device_type] = identity
        for device_type, identity in parsed["last_identities"].items():
            if device_type in self._active_identities:
                continue
            record = self._identities.get(identity)
            if record is not None and device_type in record["profiles"]:
                self._active_identities[device_type] = identity
        self._last_device = None
        return self

    def _parse_blob(self, blob):
        if isinstance(blob, InputRemapProfile):
            blob = blob.save_profile()
        if isinstance(blob, (str, bytes, bytearray)):
            try:
                blob = json.loads(blob)
            except (TypeError, ValueError) as exc:
                raise InputError("invalid_profile", "profile is not valid JSON") from exc
        if not isinstance(blob, dict):
            raise InputError("invalid_profile", "profile must be an object")
        if "profile_version" not in blob:
            raise InputError(
                UNSUPPORTED_PROFILE_VERSION, "profile has no version"
            )
        if blob["profile_version"] != self.profile_version:
            raise InputError(
                UNSUPPORTED_PROFILE_VERSION,
                "expected version %s, got %s"
                % (self.profile_version, blob["profile_version"]),
            )
        for required in ("type_profiles", "identities", "guid_map"):
            if required not in blob:
                raise InputError(
                    "invalid_profile", "profile is missing %s" % required
                )

        combo_window = _number(
            blob.get("combo_window", self.combo_window), "combo_window"
        )
        if combo_window < 0:
            raise InputError(
                "invalid_combo_window", "combo window cannot be negative"
            )
        default_deadzone = _number(
            blob.get("default_deadzone", self.default_deadzone),
            "default_deadzone",
        )
        if default_deadzone < 0.0 or default_deadzone > 1.0:
            raise InputError(
                "invalid_deadzone", "deadzone must be between 0 and 1"
            )

        raw_type_profiles = blob.get("type_profiles")
        if not isinstance(raw_type_profiles, dict):
            raise InputError("invalid_profile", "type_profiles must be an object")
        type_profiles = {}
        for device_type, raw_profile in raw_type_profiles.items():
            type_profiles[_canonical_id(device_type)] = _Profile.from_dict(
                raw_profile
            )

        raw_identities = blob.get("identities")
        if not isinstance(raw_identities, dict):
            raise InputError("invalid_profile", "identities must be an object")
        identities = {}
        for identity, raw_record in raw_identities.items():
            identity = _canonical_id(identity)
            if not isinstance(raw_record, dict):
                raise InputError(
                    "invalid_profile", "identity entry must be an object"
                )
            record_type = _canonical_id(raw_record.get("device_type", ""))
            raw_profiles = raw_record.get("profiles", {})
            if not isinstance(raw_profiles, dict):
                raise InputError(
                    "invalid_profile", "identity profiles must be an object"
                )
            profiles = {}
            for device_type, raw_profile in raw_profiles.items():
                profiles[_canonical_id(device_type)] = _Profile.from_dict(
                    raw_profile
                )
            identities[identity] = {
                "device_type": record_type,
                "profiles": profiles,
            }

        raw_guid_map = blob.get("guid_map")
        if not isinstance(raw_guid_map, dict):
            raise InputError("invalid_profile", "guid_map must be an object")
        guid_map = {}
        for guid, identity in raw_guid_map.items():
            identity = _canonical_id(identity)
            if identity not in identities:
                raise InputError(
                    "invalid_profile",
                    "guid refers to an unknown controller identity",
                )
            guid_map[_canonical_id(guid)] = identity

        raw_seen = blob.get("seen_identity_types")
        if raw_seen is None:
            seen_identity_types = {
                record["device_type"] for record in identities.values()
                if record["device_type"]
            }
        else:
            if not isinstance(raw_seen, (list, tuple, set, frozenset)):
                raise InputError(
                    "invalid_profile", "seen_identity_types must be a list"
                )
            seen_identity_types = {
                _canonical_id(item) for item in raw_seen
            }

        last_identities = {}
        if "last_identities" in blob:
            raw_last_identities = blob["last_identities"]
            if raw_last_identities is None:
                raw_last_identities = {}
            if not isinstance(raw_last_identities, dict):
                raise InputError(
                    "invalid_profile", "last_identities must be an object"
                )
            for device_type, identity in raw_last_identities.items():
                device_type = _canonical_id(device_type)
                identity = _canonical_id(identity)
                record = identities.get(identity)
                if record is None or device_type not in record["profiles"]:
                    raise InputError(
                        "invalid_profile",
                        "last identity has no profile for its device type",
                    )
                last_identities[device_type] = identity
        else:
            candidates = defaultdict(set)
            for identity, record in identities.items():
                for device_type in record["profiles"]:
                    candidates[device_type].add(identity)
            for device_type, identities_for_type in candidates.items():
                if len(identities_for_type) == 1:
                    last_identities[device_type] = next(
                        iter(identities_for_type)
                    )

        raw_overrides = blob.get("game_overrides", {})
        if raw_overrides is None:
            raw_overrides = {}
        if not isinstance(raw_overrides, dict):
            raise InputError(
                "invalid_profile", "game_overrides must be an object"
            )
        game_overrides = {}
        for game_id, raw_override in raw_overrides.items():
            game_id = _canonical_id(game_id)
            if not isinstance(raw_override, dict):
                raise InputError(
                    "invalid_profile", "game override must be an object"
                )
            game_overrides[game_id] = _Profile.from_dict(raw_override)

        return {
            "combo_window": combo_window,
            "default_deadzone": default_deadzone,
            "type_profiles": type_profiles,
            "identities": identities,
            "guid_map": guid_map,
            "seen_identity_types": seen_identity_types,
            "last_identities": last_identities,
            "game_overrides": game_overrides,
        }

    # ------------------------------------------------------------------
    # Cloud merge
    # ------------------------------------------------------------------

    def sync_cloud(self, remote):
        parsed = self._parse_blob(remote)

        # Merge into a copy so a conflict can never leave a half-applied sync.
        before = self.binding_count()
        merged = copy.deepcopy(self)
        merged._merge_parsed(parsed)
        added = merged.binding_count() - before
        self.__dict__.update(merged.__dict__)
        return {"added": added, "conflicts": []}

    def _merge_parsed(self, parsed):
        added = 0

        for device_type, remote_profile in parsed["type_profiles"].items():
            local = self._type_profiles.get(device_type)
            if local is None:
                self._type_profiles[device_type] = remote_profile.clone()
                added += len(remote_profile.bindings)
            else:
                added += self._merge_profile(local, remote_profile)

            # A device type's shared profile is the template for its
            # controllers.  Merge it into every saved controller profile too,
            # so a synced setting takes effect immediately (and is present
            # after the next hotplug) instead of waiting for a keypress.
            for identity_record in self._identities.values():
                identity_profile = identity_record["profiles"].get(device_type)
                if identity_profile is not None and identity_profile is not local:
                    added += self._merge_profile(
                        identity_profile, remote_profile
                    )

        for identity, remote_record in parsed["identities"].items():
            local_record = self._identities.get(identity)
            if local_record is None:
                self._identities[identity] = {
                    "device_type": remote_record["device_type"],
                    "profiles": {
                        device_type: profile.clone()
                        for device_type, profile
                        in remote_record["profiles"].items()
                    },
                }
                added += sum(
                    len(profile.bindings)
                    for profile in remote_record["profiles"].values()
                )
                continue
            for device_type, remote_profile in remote_record["profiles"].items():
                local = local_record["profiles"].get(device_type)
                if local is None:
                    local_record["profiles"][device_type] = (
                        remote_profile.clone()
                    )
                    added += len(remote_profile.bindings)
                else:
                    added += self._merge_profile(local, remote_profile)

        for guid, identity in parsed["guid_map"].items():
            local_identity = self._guid_to_identity.get(guid)
            if local_identity is None:
                if identity not in self._identities:
                    raise InputError(
                        "invalid_profile",
                        "remote guid refers to an unknown identity",
                    )
                self._guid_to_identity[guid] = identity
            # A GUID is a stable identity alias.  If two devices disagree about
            # the same GUID, keep the local alias instead of silently moving
            # an existing controller's profile.

        self._seen_identity_types.update(parsed["seen_identity_types"])
        for device_type, identity in parsed["last_identities"].items():
            if device_type in self._active_identities:
                continue
            record = self._identities.get(identity)
            if record is not None and device_type in record["profiles"]:
                self._active_identities[device_type] = identity

        for game_id, remote_override in parsed["game_overrides"].items():
            local = self._game_overrides.get(game_id)
            if local is None:
                self._game_overrides[game_id] = remote_override.clone()
                added += len(remote_override.bindings)
            else:
                added += self._merge_profile(local, remote_override)
        return added

    def _merge_profile(self, local, remote):
        added = 0
        for physical, action in remote.bindings.items():
            if physical not in local.bindings:
                local.bindings[physical] = action
                added += 1
                continue
            old = local.bindings[physical]
            if old != action:
                raise BindingConflict(
                    BINDING_CONFLICT,
                    "%s is bound to %s locally and %s remotely"
                    % (physical, old, action),
                )

        for members, (physicals, action) in remote.combos.items():
            old = local.combos.get(members)
            if old is None:
                local.combos[members] = (physicals, action)
            elif old[1] != action:
                raise BindingConflict(
                    BINDING_CONFLICT,
                    "combo %s is bound to %s locally and %s remotely"
                    % ("+".join(sorted(members)), old[1], action),
                )

        if remote.deadzone is not None:
            if local.deadzone is None:
                local.deadzone = remote.deadzone
            # If both sides explicitly set a deadzone, the local value wins;
            # a deadzone is a preference, not an additional binding.
        return added

    # ------------------------------------------------------------------
    # Internal profile lookup
    # ------------------------------------------------------------------

    def _profile(self, device_type):
        identity = self._active_identities.get(device_type)
        if identity is not None:
            record = self._identities.get(identity)
            if record is not None:
                profile = record["profiles"].get(device_type)
                if profile is not None:
                    return profile
        return self._type_profiles.setdefault(device_type, _Profile())

    def _writable_profile(self, device_type):
        return self._profile(device_type)

    def binding_count(self):
        entries = defaultdict(set)
        for device_type, profile in self._type_profiles.items():
            for physical, action in profile.bindings.items():
                entries[device_type].add(("single", physical, action))
            for members, (_, action) in profile.combos.items():
                entries[device_type].add(
                    ("combo", tuple(sorted(members)), action)
                )
        for record in self._identities.values():
            for device_type, profile in record["profiles"].items():
                for physical, action in profile.bindings.items():
                    entries[device_type].add(("single", physical, action))
                for members, (_, action) in profile.combos.items():
                    entries[device_type].add(
                        ("combo", tuple(sorted(members)), action)
                    )
        return sum(len(items) for items in entries.values())
