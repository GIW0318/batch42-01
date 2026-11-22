import copy
import heapq


class AuraStateError(Exception):
    pass


class AuraScheduler:
    def __init__(self, max_auras=32):
        if max_auras is not None and max_auras < 0:
            raise AuraStateError("max_auras must be non-negative")
        self.max_auras = max_auras
        self._definitions = {}
        self._instances = {}
        self._tick_queue = []
        self._next_instance = 1
        self._applied_events = {}
        self._paused = False
        self._clock_ms = 0
        self._raw_ms = 0
        self._dead = set()
        self._immune = set()
        self._haste = {}

    def define_aura(self, aura_id, name, kind="buff", max_stacks=1,
                    duration_ms=1000, stack_mode="refresh", tick_ms=None,
                    stack_bonus=0):
        if max_stacks < 1:
            raise AuraStateError("max_stacks must be at least one")
        if duration_ms < 0:
            raise AuraStateError("duration_ms must be non-negative")
        if tick_ms is not None and tick_ms <= 0:
            raise AuraStateError("tick_ms must be positive")
        if stack_mode not in ("refresh", "stack"):
            raise AuraStateError("unknown stack_mode: %s" % (stack_mode,))
        self._definitions[aura_id] = {
            "aura_id": aura_id,
            "name": name,
            "kind": kind,
            "max_stacks": max_stacks,
            "duration_ms": duration_ms,
            "stack_mode": stack_mode,
            "tick_ms": tick_ms,
            "stack_bonus": stack_bonus,
        }

    def apply(self, event_id, aura_id, target_id, source_id="self", now_ms=0):
        now = self._advance_clock(now_ms)
        self._expire_due(now)
        if event_id in self._applied_events:
            return self._applied_events[event_id]
        if aura_id not in self._definitions:
            raise AuraStateError("unknown aura id: %s" % (aura_id,))

        definition = self._definitions[aura_id]
        if target_id in self._dead:
            result = False
            self._applied_events[event_id] = result
            return result
        if target_id in self._immune and definition["kind"] != "buff":
            result = False
            self._applied_events[event_id] = result
            return result

        instance = self._find_instance(target_id, aura_id, source_id)
        if instance is None:
            if (self.max_auras is not None
                    and self._instance_count(target_id) >= self.max_auras):
                raise AuraStateError("aura limit exceeded for target: %s"
                                     % (target_id,))
            instance = self._create_instance(definition, target_id, source_id,
                                             now)
            self._instances[instance["instance_id"]] = instance
            self._schedule_instance(instance)
        else:
            if (definition["stack_mode"] == "stack"
                    and instance["stacks"] >= definition["max_stacks"]):
                raise AuraStateError("stack limit exceeded for aura: %s"
                                     % (aura_id,))
            if definition["stack_mode"] == "stack":
                instance["stacks"] += 1
            instance["expires_at_ms"] = now + definition["duration_ms"]
            self._reschedule_after_refresh(instance, now)

        instance["last_event_id"] = event_id
        result = instance["instance_id"]
        self._applied_events[event_id] = result
        return result

    def remove_instance(self, instance_id, now_ms=0):
        now = self._advance_clock(now_ms)
        self._expire_due(now)
        return self._remove_instance(instance_id) is not None

    def dispel(self, target_id, category, count=1, now_ms=None):
        now = self._current_time(now_ms)
        self._expire_due(now)
        if count < 0:
            raise AuraStateError("count must be non-negative")
        candidates = [
            instance for instance in self._instances.values()
            if instance["target_id"] == target_id
            and instance["kind"] == category
        ]
        candidates.sort(key=lambda item: (item["expires_at_ms"],
                                          item["instance_id"]))
        removed = []
        for instance in candidates[:count]:
            removed.append(instance["aura_id"])
            self._remove_instance(instance["instance_id"])
        return removed

    def kill(self, target_id, now_ms=None):
        now = self._current_time(now_ms)
        self._expire_due(now)
        self._dead.add(target_id)
        removed = []
        for instance in self._target_instances(target_id):
            removed.append(instance["aura_id"])
            self._remove_instance(instance["instance_id"])
        return removed

    def revive(self, target_id, now_ms=None):
        now = self._current_time(now_ms)
        self._expire_due(now)
        self._dead.discard(target_id)

    def set_paused(self, paused, now_ms=None):
        if now_ms is not None:
            now = self._advance_clock(now_ms)
            self._expire_due(now)
        self._paused = bool(paused)

    def set_immune(self, target_id, immune):
        if immune:
            self._immune.add(target_id)
        else:
            self._immune.discard(target_id)

    def set_haste(self, target_id, multiplier, now_ms=None):
        if multiplier <= 0:
            raise AuraStateError("haste multiplier must be positive")
        now = self._current_time(now_ms)
        self._haste[target_id] = multiplier
        for instance in self._target_instances(target_id):
            if instance["tick_ms"] is None:
                continue
            anchor = instance["last_tick_at_ms"]
            if anchor is None:
                anchor = instance["applied_at_ms"]
            next_tick = anchor + self._tick_interval(instance)
            next_tick = min(next_tick, instance["expires_at_ms"])
            self._schedule_instance(instance, next_tick)
        self._expire_due(now)

    def travel(self, target_id, now_ms=None):
        now = self._current_time(now_ms)
        self._expire_due(now)
        removed = []
        for instance in self._target_instances(target_id):
            removed.append(instance["aura_id"])
            self._remove_instance(instance["instance_id"])
        return removed

    def tick(self, now_ms):
        now = self._advance_clock(now_ms)
        events = []
        while self._tick_queue and self._tick_queue[0][0] <= now:
            at_ms, instance_id, generation = heapq.heappop(self._tick_queue)
            instance = self._instances.get(instance_id)
            if instance is None or instance["tick_generation"] != generation:
                continue

            if instance["tick_ms"] is None:
                events.append(self._make_event(instance, at_ms, "expire"))
                self._remove_instance(instance_id)
                continue

            instance["last_tick_at_ms"] = at_ms
            expired = at_ms >= instance["expires_at_ms"]
            event = self._make_event(instance, at_ms, "tick")
            event["expired"] = expired
            events.append(event)
            if expired:
                self._remove_instance(instance_id)
                continue

            next_tick = at_ms + self._tick_interval(instance)
            next_tick = min(next_tick, instance["expires_at_ms"])
            if next_tick <= at_ms:
                next_tick = instance["expires_at_ms"]
            self._schedule_instance(instance, next_tick)

        events.sort(key=lambda event: (
            event["at_ms"],
            event["instance_id"],
            0 if event["type"] == "tick" else 1,
        ))
        return events

    def stacks(self, target_id, aura_id):
        if aura_id not in self._definitions:
            raise AuraStateError("unknown aura id: %s" % (aura_id,))
        self._expire_due(self._clock_ms)
        return sum(
            instance["stacks"]
            for instance in self._target_instances(target_id)
            if instance["aura_id"] == aura_id
        )

    def remaining_ms(self, instance_id, now_ms):
        now = self._advance_clock(now_ms)
        self._expire_due(now)
        instance = self._instances.get(instance_id)
        if instance is None:
            return 0
        return instance["expires_at_ms"] - now

    def stat_bonus(self, target_id):
        self._expire_due(self._clock_ms)
        return sum(
            instance["stacks"] * instance["stack_bonus"]
            for instance in self._target_instances(target_id)
        )

    def instances(self, target_id):
        self._expire_due(self._clock_ms)
        result = []
        for instance in sorted(
                self._target_instances(target_id),
                key=lambda item: (item["expires_at_ms"], item["instance_id"])):
            result.append({
                "instance_id": instance["instance_id"],
                "aura_id": instance["aura_id"],
                "name": instance["name"],
                "kind": instance["kind"],
                "target_id": instance["target_id"],
                "source_id": instance["source_id"],
                "stacks": instance["stacks"],
                "applied_at_ms": instance["applied_at_ms"],
                "expires_at_ms": instance["expires_at_ms"],
            })
        return result

    def snapshot(self):
        self._expire_due(self._clock_ms)
        instances = []
        for instance in sorted(self._instances.values(),
                               key=lambda item: item["instance_id"]):
            saved = copy.deepcopy(instance)
            saved["remaining_ms"] = max(
                0, instance["expires_at_ms"] - self._clock_ms)
            instances.append(saved)
        return {
            "version": 1,
            "max_auras": self.max_auras,
            "clock_ms": self._clock_ms,
            "raw_ms": self._raw_ms,
            "paused": self._paused,
            "next_instance": self._next_instance,
            "definitions": copy.deepcopy(self._definitions),
            "instances": instances,
            "applied_events": [
                [event_id, result]
                for event_id, result in self._applied_events.items()
            ],
            "dead": list(self._dead),
            "immune": list(self._immune),
            "haste": copy.deepcopy(self._haste),
        }

    def restore(self, snapshot):
        if not isinstance(snapshot, dict):
            raise AuraStateError("snapshot must be a dictionary")

        self.max_auras = snapshot.get("max_auras", self.max_auras)
        self._definitions = copy.deepcopy(snapshot.get("definitions", {}))
        self._instances = {}
        self._tick_queue = []
        self._clock_ms = snapshot.get("clock_ms", 0)
        self._raw_ms = max(snapshot.get("raw_ms", self._clock_ms),
                           self._clock_ms)
        self._paused = bool(snapshot.get("paused", False))
        self._next_instance = snapshot.get("next_instance", 1)
        self._applied_events = {}
        for item in snapshot.get("applied_events", []):
            if isinstance(item, dict):
                self._applied_events[item["event_id"]] = item["result"]
            else:
                self._applied_events[item[0]] = item[1]
        self._dead = set(snapshot.get("dead", []))
        self._immune = set(snapshot.get("immune", []))
        self._haste = copy.deepcopy(snapshot.get("haste", {}))

        highest_id = 0
        for saved in snapshot.get("instances", []):
            instance = copy.deepcopy(saved)
            instance.pop("remaining_ms", None)
            if "instance_id" not in instance or "expires_at_ms" not in instance:
                raise AuraStateError("invalid instance in snapshot")
            instance_id = instance["instance_id"]
            if instance_id in self._instances:
                raise AuraStateError("duplicate instance id in snapshot")
            instance.setdefault("tick_generation", 0)
            instance.setdefault("next_tick_at_ms",
                                instance.get("expires_at_ms"))
            self._instances[instance_id] = instance
            highest_id = max(highest_id, instance_id)
            if instance.get("tick_ms") is not None:
                heapq.heappush(self._tick_queue, (
                    instance["next_tick_at_ms"],
                    instance_id,
                    instance["tick_generation"],
                ))
            else:
                heapq.heappush(self._tick_queue, (
                    instance["expires_at_ms"],
                    instance_id,
                    instance["tick_generation"],
                ))
        self._next_instance = max(self._next_instance, highest_id + 1)

    def _advance_clock(self, now_ms):
        if now_ms is None:
            return self._clock_ms
        if now_ms > self._raw_ms:
            if not self._paused:
                self._clock_ms += now_ms - self._raw_ms
            self._raw_ms = now_ms
        return self._clock_ms

    def _current_time(self, now_ms):
        if now_ms is None:
            return self._clock_ms
        return self._advance_clock(now_ms)

    def _create_instance(self, definition, target_id, source_id, now):
        instance = {
            "instance_id": self._next_instance,
            "aura_id": definition["aura_id"],
            "name": definition["name"],
            "kind": definition["kind"],
            "target_id": target_id,
            "source_id": source_id,
            "stacks": 1,
            "max_stacks": definition["max_stacks"],
            "stack_mode": definition["stack_mode"],
            "duration_ms": definition["duration_ms"],
            "tick_ms": definition["tick_ms"],
            "stack_bonus": definition["stack_bonus"],
            "applied_at_ms": now,
            "expires_at_ms": now + definition["duration_ms"],
            "last_tick_at_ms": None,
            "next_tick_at_ms": None,
            "tick_generation": 0,
            "last_event_id": None,
        }
        self._next_instance += 1
        return instance

    def _find_instance(self, target_id, aura_id, source_id):
        for instance in self._instances.values():
            if (instance["target_id"] == target_id
                    and instance["aura_id"] == aura_id
                    and instance["source_id"] == source_id):
                return instance
        return None

    def _target_instances(self, target_id):
        return [
            instance for instance in self._instances.values()
            if instance["target_id"] == target_id
        ]

    def _instance_count(self, target_id):
        return sum(
            1 for instance in self._instances.values()
            if instance["target_id"] == target_id
        )

    def _tick_interval(self, instance):
        multiplier = self._haste.get(instance["target_id"], 1.0)
        return instance["tick_ms"] / multiplier

    def _schedule_instance(self, instance, next_tick=None):
        if instance["tick_ms"] is None:
            instance["next_tick_at_ms"] = instance["expires_at_ms"]
            due_at = instance["expires_at_ms"]
        else:
            if next_tick is None:
                next_tick = instance["applied_at_ms"] + self._tick_interval(
                    instance)
                next_tick = min(next_tick, instance["expires_at_ms"])
            instance["next_tick_at_ms"] = next_tick
            due_at = next_tick
        instance["tick_generation"] += 1
        heapq.heappush(self._tick_queue, (
            due_at,
            instance["instance_id"],
            instance["tick_generation"],
        ))

    def _reschedule_after_refresh(self, instance, now):
        if instance["tick_ms"] is None:
            self._schedule_instance(instance)
            return
        next_tick = instance.get("next_tick_at_ms")
        if next_tick is None or next_tick > instance["expires_at_ms"]:
            anchor = now
            next_tick = min(
                anchor + self._tick_interval(instance),
                instance["expires_at_ms"],
            )
        self._schedule_instance(instance, next_tick)

    def _remove_instance(self, instance_id):
        instance = self._instances.pop(instance_id, None)
        if instance is None:
            return None
        instance["tick_generation"] += 1
        return instance

    def _expire_due(self, now):
        expired = [
            instance_id
            for instance_id, instance in self._instances.items()
            if instance["expires_at_ms"] <= now
        ]
        for instance_id in expired:
            self._remove_instance(instance_id)

    def _make_event(self, instance, at_ms, event_type):
        return {
            "type": event_type,
            "at_ms": at_ms,
            "instance_id": instance["instance_id"],
            "aura_id": instance["aura_id"],
            "target_id": instance["target_id"],
            "source_id": instance["source_id"],
            "stacks": instance["stacks"],
        }
