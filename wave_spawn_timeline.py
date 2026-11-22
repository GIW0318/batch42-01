import math
from copy import deepcopy


class WaveConfigError(Exception):
    pass


class WaveTimeline:
    """Deterministic, checkpointable wave scheduler.

    Wave definitions are immutable inputs, ``_pending`` is the spawn queue, and
    the monster maps/sets form the settlement state.  Only one wave can be live
    at a time, which prevents bosses and normal waves from sharing a frame.
    """

    _SNAPSHOT_VERSION = 1
    _FLOAT_EPSILON = 1e-9

    def __init__(self, season_id, frame_budget=8):
        if not isinstance(season_id, str) or not season_id.strip():
            raise WaveConfigError("season_id must be a non-empty string")
        if (
            isinstance(frame_budget, bool)
            or not isinstance(frame_budget, int)
            or frame_budget < 1
        ):
            raise WaveConfigError("frame_budget must be a positive integer")

        self.season_id = season_id
        self.frame_budget = frame_budget

        self._waves = []
        self._wave_by_id = {}
        self._clock_ms = 0.0
        self._paused = False
        self._speed = 1.0

        self._pending = []
        self._alive = {}
        self._killed = set()
        self._leaked = set()

        self._claimed = set()
        self._triggered = set()
        self._completed = set()

        self._current_wave_id = None
        self._next_wave_index = 0
        self._wave_started_ms = None
        self._lane_cursor = 0
        self._spawned_per_wave = {}
        self._total_spawned = 0

    @staticmethod
    def _is_real_number(value):
        return (
            not isinstance(value, bool)
            and isinstance(value, (int, float))
            and math.isfinite(value)
        )

    @classmethod
    def _normalise_waves(cls, waves):
        if isinstance(waves, (str, bytes, dict)):
            raise WaveConfigError("waves must be a sequence of definitions")
        try:
            definitions = list(waves)
        except TypeError as exc:
            raise WaveConfigError("waves must be a sequence of definitions") from exc
        if not definitions:
            raise WaveConfigError("at least one wave definition is required")

        normalised = []
        seen_ids = set()
        for definition in definitions:
            if not isinstance(definition, dict):
                raise WaveConfigError("each wave definition must be a mapping")

            wave_id = definition.get("wave_id")
            if (
                isinstance(wave_id, bool)
                or not isinstance(wave_id, int)
                or wave_id < 1
            ):
                raise WaveConfigError("wave_id must be a positive integer")
            if wave_id in seen_ids:
                raise WaveConfigError(f"duplicate wave_id: {wave_id}")
            seen_ids.add(wave_id)

            count = definition.get("count")
            if (
                isinstance(count, bool)
                or not isinstance(count, int)
                or count < 1
            ):
                raise WaveConfigError(
                    f"wave {wave_id} count must be a positive integer"
                )

            interval_ms = definition.get("interval_ms")
            if (
                not cls._is_real_number(interval_ms)
                or interval_ms < 0
            ):
                raise WaveConfigError(
                    f"wave {wave_id} interval_ms must be non-negative"
                )

            lanes = definition.get("lanes")
            if isinstance(lanes, (str, bytes, dict)):
                raise WaveConfigError(f"wave {wave_id} lanes must be a sequence")
            try:
                lanes = list(lanes)
            except TypeError as exc:
                raise WaveConfigError(
                    f"wave {wave_id} lanes must be a sequence"
                ) from exc
            if not lanes:
                raise WaveConfigError(f"wave {wave_id} must have at least one lane")
            if any(not isinstance(lane, str) or not lane.strip() for lane in lanes):
                raise WaveConfigError(
                    f"wave {wave_id} lanes must be non-empty strings"
                )
            if len(set(lanes)) != len(lanes):
                raise WaveConfigError(f"wave {wave_id} has duplicate lanes")

            boss = definition.get("boss", False)
            if not isinstance(boss, bool):
                raise WaveConfigError(f"wave {wave_id} boss must be a boolean")

            normalised_definition = deepcopy(definition)
            normalised_definition["wave_id"] = wave_id
            normalised_definition["count"] = count
            normalised_definition["interval_ms"] = interval_ms
            normalised_definition["lanes"] = lanes
            normalised_definition["boss"] = boss
            normalised.append(normalised_definition)

        return normalised

    def _reset_runtime_state(self):
        self._clock_ms = 0.0
        self._paused = False
        self._speed = 1.0
        self._pending = []
        self._alive = {}
        self._killed = set()
        self._leaked = set()
        self._claimed = set()
        self._triggered = set()
        self._completed = set()
        self._current_wave_id = None
        self._next_wave_index = 0
        self._wave_started_ms = None
        self._lane_cursor = 0
        self._spawned_per_wave = {}
        self._total_spawned = 0

    def load_waves(self, waves):
        definitions = self._normalise_waves(waves)
        self._waves = definitions
        self._wave_by_id = {
            definition["wave_id"]: definition for definition in definitions
        }
        self._reset_runtime_state()

    def _key(self, wave_id):
        return self.season_id, wave_id

    def _is_wave_complete(self, wave_id):
        return wave_id in self._completed

    def _current_wave_is_complete(self):
        return (
            self._current_wave_id is not None
            and self._is_wave_complete(self._current_wave_id)
        )

    def _activate_wave_at(self, index):
        if index < 0 or index >= len(self._waves):
            return False

        definition = self._waves[index]
        wave_id = definition["wave_id"]
        key = self._key(wave_id)
        if key in self._triggered:
            return False
        if (
            self._current_wave_id is not None
            and not self._current_wave_is_complete()
        ):
            return False

        self._triggered.add(key)
        self._current_wave_id = wave_id
        self._wave_started_ms = self._clock_ms
        self._lane_cursor = 0
        self._spawned_per_wave[wave_id] = 0
        self._next_wave_index = max(self._next_wave_index, index + 1)

        lanes = definition["lanes"]
        interval_ms = definition["interval_ms"]
        for spawn_index in range(definition["count"]):
            self._pending.append(
                {
                    "wave_id": wave_id,
                    "lane": lanes[spawn_index % len(lanes)],
                    "spawn_index": spawn_index,
                    "due_ms": self._clock_ms + spawn_index * interval_ms,
                }
            )
        self._pending.sort(
            key=lambda entry: (
                entry["due_ms"],
                entry["wave_id"],
                entry["spawn_index"],
            )
        )
        return True

    def _auto_start_next_wave(self):
        if (
            self._current_wave_id is not None
            and not self._current_wave_is_complete()
        ):
            return False

        index = self._next_wave_index
        while index < len(self._waves):
            wave_id = self._waves[index]["wave_id"]
            if self._key(wave_id) not in self._triggered:
                break
            index += 1
        self._next_wave_index = index

        if index >= len(self._waves):
            return False
        return self._activate_wave_at(index)

    def trigger_wave(self, wave_id):
        if isinstance(wave_id, bool) or not isinstance(wave_id, int):
            return False
        definition = self._wave_by_id.get(wave_id)
        if definition is None:
            return False
        if self._key(wave_id) in self._triggered:
            return False
        if (
            self._current_wave_id is not None
            and not self._current_wave_is_complete()
        ):
            return False

        index = self._waves.index(definition)
        return self._activate_wave_at(index)

    def update(self, dt_ms):
        if not self._is_real_number(dt_ms) or dt_ms < 0:
            raise WaveConfigError("dt_ms must be a non-negative finite number")
        if self._paused:
            return []

        # A new wave starts at the current logical instant. Only the delta below
        # is scaled by speed, so changing speed never inserts or removes time.
        if (
            self._current_wave_id is None
            or self._current_wave_is_complete()
        ):
            self._auto_start_next_wave()

        self._clock_ms += dt_ms * self._speed
        spawned = []

        while (
            len(spawned) < self.frame_budget
            and self._pending
            and self._pending[0]["due_ms"]
            <= self._clock_ms + self._FLOAT_EPSILON
        ):
            queued = self._pending.pop(0)
            wave_id = queued["wave_id"]
            spawn_index = queued["spawn_index"]
            monster_id = f"{self.season_id}:w{wave_id}:m{spawn_index}"
            monster = {
                "monster_id": monster_id,
                "wave_id": wave_id,
                "lane": queued["lane"],
                "spawn_index": spawn_index,
                "spawned_at_ms": self._clock_ms,
            }
            self._alive[monster_id] = monster
            self._total_spawned += 1
            self._spawned_per_wave[wave_id] += 1
            self._lane_cursor = (spawn_index + 1) % len(
                self._wave_by_id[wave_id]["lanes"]
            )
            spawned.append(dict(monster))

        return spawned

    def now_ms(self):
        return self._clock_ms

    def set_paused(self, paused):
        if not isinstance(paused, bool):
            raise WaveConfigError("paused must be a boolean")
        self._paused = paused

    def set_speed(self, speed):
        if not self._is_real_number(speed) or speed <= 0:
            raise WaveConfigError("speed must be a positive finite number")
        self._speed = speed

    def stats(self):
        return {
            "season_id": self.season_id,
            "wave": (
                self._current_wave_id
                if self._current_wave_id is not None
                else 0
            ),
            "spawned": self._total_spawned,
            "alive": len(self._alive),
            "killed": len(self._killed),
            "leaked": len(self._leaked),
            "cleared": self._current_wave_is_complete(),
            "pending": len(self._pending),
            "claimed": len(self._claimed),
            "clock_ms": self._clock_ms,
            "paused": self._paused,
            "speed": self._speed,
        }

    def _check_wave_completion(self, wave_id):
        if wave_id in self._completed:
            return
        definition = self._wave_by_id.get(wave_id)
        if definition is None:
            return
        if self._spawned_per_wave.get(wave_id, 0) != definition["count"]:
            return
        if any(entry["wave_id"] == wave_id for entry in self._pending):
            return
        if any(
            monster["wave_id"] == wave_id
            for monster in self._alive.values()
        ):
            return
        self._completed.add(wave_id)

    def _resolve_monster(self, monster_id, destination):
        if not isinstance(monster_id, str):
            return False
        monster = self._alive.pop(monster_id, None)
        if monster is None:
            return False
        destination.add(monster_id)
        self._check_wave_completion(monster["wave_id"])
        return True

    def kill(self, monster_id):
        return self._resolve_monster(monster_id, self._killed)

    def leak(self, monster_id):
        return self._resolve_monster(monster_id, self._leaked)

    def claim_reward(self, wave_id):
        if isinstance(wave_id, bool) or not isinstance(wave_id, int):
            return False
        if wave_id not in self._completed:
            return False
        key = self._key(wave_id)
        if key in self._claimed:
            return False
        self._claimed.add(key)
        return True

    def snapshot(self):
        return {
            "version": self._SNAPSHOT_VERSION,
            "season_id": self.season_id,
            "frame_budget": self.frame_budget,
            "waves": deepcopy(self._waves),
            "clock_ms": self._clock_ms,
            "paused": self._paused,
            "speed": self._speed,
            "current_wave_id": self._current_wave_id,
            "next_wave_index": self._next_wave_index,
            "wave_started_ms": self._wave_started_ms,
            "lane_cursor": self._lane_cursor,
            "pending": deepcopy(self._pending),
            "alive": deepcopy(list(self._alive.values())),
            "killed_monster_ids": sorted(self._killed),
            "leaked_monster_ids": sorted(self._leaked),
            "claimed_wave_ids": sorted(
                wave_id
                for season_id, wave_id in self._claimed
                if season_id == self.season_id
            ),
            "triggered_wave_ids": sorted(
                wave_id
                for season_id, wave_id in self._triggered
                if season_id == self.season_id
            ),
            "completed_wave_ids": sorted(self._completed),
            "spawned_per_wave": [
                [wave_id, count]
                for wave_id, count in sorted(self._spawned_per_wave.items())
            ],
            "total_spawned": self._total_spawned,
        }

    @classmethod
    def _read_number(cls, snapshot, field, minimum=0, allow_equal=True):
        value = snapshot.get(field)
        if not cls._is_real_number(value):
            raise WaveConfigError(f"snapshot {field} must be a finite number")
        valid = value >= minimum if allow_equal else value > minimum
        if not valid:
            raise WaveConfigError(f"snapshot {field} is out of range")
        return value

    @classmethod
    def _read_int(cls, snapshot, field, minimum=0):
        value = snapshot.get(field)
        if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
            raise WaveConfigError(f"snapshot {field} must be an integer")
        return value

    @classmethod
    def _read_id_list(cls, snapshot, field):
        values = snapshot.get(field, [])
        if not isinstance(values, list):
            raise WaveConfigError(f"snapshot {field} must be a list")
        if any(
            isinstance(value, bool)
            or not isinstance(value, int)
            or value < 1
            for value in values
        ):
            raise WaveConfigError(f"snapshot {field} contains an invalid id")
        if len(set(values)) != len(values):
            raise WaveConfigError(f"snapshot {field} contains duplicate ids")
        return set(values)

    @classmethod
    def _read_monster_ids(cls, snapshot, field):
        values = snapshot.get(field, [])
        if not isinstance(values, list):
            raise WaveConfigError(f"snapshot {field} must be a list")
        if any(not isinstance(value, str) or not value for value in values):
            raise WaveConfigError(f"snapshot {field} contains an invalid id")
        if len(set(values)) != len(values):
            raise WaveConfigError(f"snapshot {field} contains duplicate ids")
        return set(values)

    @classmethod
    def _read_pending(cls, raw_entries, wave_by_id):
        if not isinstance(raw_entries, list):
            raise WaveConfigError("snapshot pending must be a list")

        entries = []
        seen_slots = set()
        for raw in raw_entries:
            if not isinstance(raw, dict):
                raise WaveConfigError("snapshot pending is malformed")
            wave_id = raw.get("wave_id")
            lane = raw.get("lane")
            spawn_index = raw.get("spawn_index")
            due_ms = raw.get("due_ms")
            if wave_id not in wave_by_id:
                raise WaveConfigError("snapshot pending has an unknown wave")
            if (
                isinstance(spawn_index, bool)
                or not isinstance(spawn_index, int)
                or spawn_index < 0
            ):
                raise WaveConfigError("snapshot pending has an invalid spawn index")
            definition = wave_by_id[wave_id]
            if spawn_index >= definition["count"]:
                raise WaveConfigError("snapshot pending spawn index is out of range")
            expected_lane = definition["lanes"][
                spawn_index % len(definition["lanes"])
            ]
            if lane != expected_lane:
                raise WaveConfigError("snapshot pending has an invalid lane")
            if not cls._is_real_number(due_ms) or due_ms < 0:
                raise WaveConfigError("snapshot pending has an invalid due time")

            slot = (wave_id, spawn_index)
            if slot in seen_slots:
                raise WaveConfigError("snapshot pending contains duplicates")
            seen_slots.add(slot)
            entries.append(
                {
                    "wave_id": wave_id,
                    "lane": lane,
                    "spawn_index": spawn_index,
                    "due_ms": due_ms,
                }
            )
        return entries, seen_slots

    @classmethod
    def _read_spawned_counts(cls, snapshot, wave_ids):
        raw_counts = snapshot.get("spawned_per_wave", [])
        if isinstance(raw_counts, dict):
            raw_counts = list(raw_counts.items())
        if not isinstance(raw_counts, list):
            raise WaveConfigError("snapshot spawned_per_wave must be a list")

        counts = {}
        for pair in raw_counts:
            if not isinstance(pair, (list, tuple)) or len(pair) != 2:
                raise WaveConfigError("snapshot spawned_per_wave is malformed")
            wave_id, count = pair
            try:
                wave_id = int(wave_id)
            except (TypeError, ValueError) as exc:
                raise WaveConfigError(
                    "snapshot spawned_per_wave has an invalid wave id"
                ) from exc
            if (
                wave_id not in wave_ids
                or isinstance(count, bool)
                or not isinstance(count, int)
                or count < 0
            ):
                raise WaveConfigError("snapshot spawned_per_wave is invalid")
            if wave_id in counts:
                raise WaveConfigError(
                    "snapshot spawned_per_wave has duplicate waves"
                )
            counts[wave_id] = count
        return counts

    @classmethod
    def _read_monsters(cls, raw_monsters, wave_by_id, field):
        if not isinstance(raw_monsters, list):
            raise WaveConfigError(f"snapshot {field} must be a list")

        monsters = []
        seen_ids = set()
        seen_slots = set()
        for raw in raw_monsters:
            if not isinstance(raw, dict):
                raise WaveConfigError(f"snapshot {field} is malformed")
            monster_id = raw.get("monster_id")
            wave_id = raw.get("wave_id")
            lane = raw.get("lane")
            spawn_index = raw.get("spawn_index")
            spawned_at_ms = raw.get("spawned_at_ms", 0)
            if not isinstance(monster_id, str) or not monster_id:
                raise WaveConfigError(f"snapshot {field} has an invalid monster id")
            if wave_id not in wave_by_id:
                raise WaveConfigError(f"snapshot {field} has an unknown wave")
            if (
                isinstance(spawn_index, bool)
                or not isinstance(spawn_index, int)
                or spawn_index < 0
            ):
                raise WaveConfigError(f"snapshot {field} has an invalid spawn index")
            definition = wave_by_id[wave_id]
            if spawn_index >= definition["count"]:
                raise WaveConfigError(f"snapshot {field} spawn index is out of range")
            expected_lane = definition["lanes"][
                spawn_index % len(definition["lanes"])
            ]
            if lane != expected_lane:
                raise WaveConfigError(f"snapshot {field} has an invalid lane")
            if not cls._is_real_number(spawned_at_ms) or spawned_at_ms < 0:
                raise WaveConfigError(
                    f"snapshot {field} has an invalid spawn timestamp"
                )

            slot = (wave_id, spawn_index)
            if monster_id in seen_ids or slot in seen_slots:
                raise WaveConfigError(f"snapshot {field} contains duplicates")
            seen_ids.add(monster_id)
            seen_slots.add(slot)
            monsters.append(
                {
                    "monster_id": monster_id,
                    "wave_id": wave_id,
                    "lane": lane,
                    "spawn_index": spawn_index,
                    "spawned_at_ms": spawned_at_ms,
                }
            )
        return monsters, seen_ids, seen_slots

    def restore(self, snapshot):
        if not isinstance(snapshot, dict):
            raise WaveConfigError("snapshot must be a mapping")

        season_id = snapshot.get("season_id")
        if not isinstance(season_id, str) or not season_id.strip():
            raise WaveConfigError("snapshot season_id is invalid")

        frame_budget = snapshot.get("frame_budget", self.frame_budget)
        if (
            isinstance(frame_budget, bool)
            or not isinstance(frame_budget, int)
            or frame_budget < 1
        ):
            raise WaveConfigError("snapshot frame_budget is invalid")

        waves_data = snapshot.get("waves", self._waves)
        definitions = self._normalise_waves(waves_data)
        wave_by_id = {
            definition["wave_id"]: definition for definition in definitions
        }

        clock_ms = self._read_number(snapshot, "clock_ms")
        speed = self._read_number(
            snapshot, "speed", minimum=0, allow_equal=False
        )
        paused = snapshot.get("paused", False)
        if not isinstance(paused, bool):
            raise WaveConfigError("snapshot paused must be a boolean")

        current_wave_id = snapshot.get("current_wave_id")
        if current_wave_id is not None and current_wave_id not in wave_by_id:
            raise WaveConfigError("snapshot current_wave_id is unknown")
        wave_started_ms = snapshot.get("wave_started_ms")
        if wave_started_ms is not None:
            if not self._is_real_number(wave_started_ms) or wave_started_ms < 0:
                raise WaveConfigError("snapshot wave_started_ms is invalid")

        next_wave_index = self._read_int(snapshot, "next_wave_index")
        if next_wave_index > len(definitions):
            raise WaveConfigError("snapshot next_wave_index is out of range")
        lane_cursor = self._read_int(snapshot, "lane_cursor")
        if current_wave_id is not None:
            lane_count = len(wave_by_id[current_wave_id]["lanes"])
            if lane_cursor >= lane_count:
                raise WaveConfigError("snapshot lane_cursor is out of range")

        pending_entries, pending_slots = self._read_pending(
            snapshot.get("pending", []), wave_by_id
        )
        alive_entries, alive_ids, alive_slots = self._read_monsters(
            snapshot.get("alive", []), wave_by_id, "alive"
        )
        if pending_slots & alive_slots:
            raise WaveConfigError(
                "snapshot contains an alive and pending duplicate"
            )

        killed = self._read_monster_ids(snapshot, "killed_monster_ids")
        leaked = self._read_monster_ids(snapshot, "leaked_monster_ids")
        if killed & leaked:
            raise WaveConfigError("snapshot killed/leaked ids overlap")
        resolved = killed | leaked
        if resolved & alive_ids:
            raise WaveConfigError(
                "snapshot resolved and live monster ids overlap"
            )

        claimed_ids = self._read_id_list(snapshot, "claimed_wave_ids")
        triggered_ids = self._read_id_list(snapshot, "triggered_wave_ids")
        completed_ids = self._read_id_list(snapshot, "completed_wave_ids")
        for wave_id in claimed_ids | triggered_ids | completed_ids:
            if wave_id not in wave_by_id:
                raise WaveConfigError("snapshot references an unknown wave")
        if not claimed_ids <= completed_ids:
            raise WaveConfigError("snapshot claims an incomplete wave")
        if current_wave_id is not None and current_wave_id not in triggered_ids:
            raise WaveConfigError("snapshot current wave was not triggered")

        spawned_counts = self._read_spawned_counts(snapshot, set(wave_by_id))
        for definition in definitions:
            wave_id = definition["wave_id"]
            spawned = spawned_counts.get(wave_id, 0)
            if spawned > definition["count"]:
                raise WaveConfigError("snapshot spawn count is out of range")
            pending_count = sum(
                1 for entry in pending_entries if entry["wave_id"] == wave_id
            )
            alive_count = sum(
                1 for entry in alive_entries if entry["wave_id"] == wave_id
            )
            if alive_count > spawned:
                raise WaveConfigError("snapshot wave counters are inconsistent")
            if spawned + pending_count > definition["count"]:
                raise WaveConfigError("snapshot wave counters are inconsistent")

        total_spawned = self._read_int(snapshot, "total_spawned")
        if total_spawned != len(alive_entries) + len(killed) + len(leaked):
            raise WaveConfigError("snapshot monster conservation is violated")
        if total_spawned != sum(spawned_counts.values()):
            raise WaveConfigError("snapshot spawned totals are inconsistent")

        for wave_id in completed_ids:
            if spawned_counts.get(wave_id, 0) != wave_by_id[wave_id]["count"]:
                raise WaveConfigError("snapshot completed wave is incomplete")
            if any(
                entry["wave_id"] == wave_id
                for entry in pending_entries + alive_entries
            ):
                raise WaveConfigError("snapshot completed wave still has monsters")

        claimed = {(season_id, wave_id) for wave_id in claimed_ids}
        triggered = {(season_id, wave_id) for wave_id in triggered_ids}

        self.season_id = season_id
        self.frame_budget = frame_budget
        self._waves = definitions
        self._wave_by_id = wave_by_id
        self._clock_ms = float(clock_ms)
        self._speed = float(speed)
        self._paused = paused
        self._current_wave_id = current_wave_id
        self._next_wave_index = next_wave_index
        self._wave_started_ms = wave_started_ms
        self._lane_cursor = lane_cursor
        self._pending = pending_entries
        self._alive = {
            monster["monster_id"]: monster for monster in alive_entries
        }
        self._killed = killed
        self._leaked = leaked
        self._claimed = claimed
        self._triggered = triggered
        self._completed = completed_ids
        self._spawned_per_wave = dict(spawned_counts)
        self._total_spawned = total_spawned
