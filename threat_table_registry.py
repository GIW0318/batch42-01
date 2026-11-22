import copy
import threading


class UnknownCombatantError(Exception):
    pass


class ThreatRegistry:
    def __init__(self, heal_multiplier=1.5, stealth_multiplier=0.5,
                 decay_per_second=0.0):
        self.heal_multiplier = float(heal_multiplier)
        self.stealth_multiplier = float(stealth_multiplier)
        self.decay_per_second = float(decay_per_second)
        self._lock = threading.RLock()
        self._combatants = {}
        self._threat = {}
        self._applied = set()
        self._next_seq = 1
        self._pending = {}
        self._taunt = None
        self._vulnerable = True
        self._paused = False
        self._join_order = 0

    def add_combatant(self, combatant_id, role="dps", joined_at=0, owner=None):
        with self._lock:
            if combatant_id in self._combatants:
                return
            self._join_order += 1
            self._combatants[combatant_id] = {
                "role": role,
                "joined_at": joined_at,
                "owner": owner,
                "dead": False,
                "stealth": False,
                "order": self._join_order,
                "parent": None,
            }
            self._threat[combatant_id] = 0.0

    def submit_event(self, event):
        with self._lock:
            event = copy.deepcopy(dict(event))
            seq = self._validate_event(event)

            if seq < self._next_seq or seq in self._applied \
                    or seq in self._pending:
                return False
            if seq > self._next_seq:
                self._pending[seq] = event
                return True

            applied = self._apply_event(event)
            self._applied.add(seq)
            self._next_seq += 1
            self._drain_pending()
            return applied

    def flush(self):
        with self._lock:
            return self._drain_pending()

    def threat(self, combatant_id):
        with self._lock:
            self._require_combatant(combatant_id)
            return self._threat[combatant_id]

    def effective_threat(self, combatant_id):
        with self._lock:
            combatant = self._require_combatant(combatant_id)
            if combatant["dead"]:
                return 0.0
            value = self._threat[combatant_id]
            if combatant["stealth"]:
                value *= self.stealth_multiplier
            return max(0.0, value)

    def current_target(self, now_ms=0):
        with self._lock:
            if self._taunt is not None:
                combatant_id, until_ms = self._taunt
                if now_ms < until_ms:
                    combatant = self._combatants.get(combatant_id)
                    if combatant is not None and not combatant["dead"]:
                        return combatant_id
                self._taunt = None

            candidates = []
            for combatant_id, combatant in self._combatants.items():
                if combatant["dead"] or combatant["stealth"]:
                    continue
                effective_threat = self.effective_threat(combatant_id)
                if effective_threat <= 0:
                    continue
                candidates.append((
                    -effective_threat,
                    combatant["joined_at"],
                    combatant["order"],
                    combatant_id,
                ))
            if not candidates:
                return None
            return min(candidates)[3]

    def taunt(self, combatant_id, until_ms):
        with self._lock:
            self._require_combatant(combatant_id)
            self._set_taunt(combatant_id, until_ms)

    def set_vulnerable(self, vulnerable):
        with self._lock:
            self._vulnerable = bool(vulnerable)

    def set_stealth(self, combatant_id, stealth):
        with self._lock:
            self._require_combatant(combatant_id)
            self._combatants[combatant_id]["stealth"] = bool(stealth)

    def spawn_add(self, parent_id, add_id):
        with self._lock:
            self._require_combatant(parent_id)
            if add_id in self._combatants:
                return
            joined_at = max(
                (combatant["joined_at"] for combatant in
                 self._combatants.values()),
                default=0,
            ) + 1
            self.add_combatant(add_id, role="add", joined_at=joined_at)
            self._combatants[add_id]["parent"] = parent_id

    def tick(self, dt_ms):
        with self._lock:
            if self._paused or dt_ms <= 0 or self.decay_per_second <= 0:
                return
            decay = self.decay_per_second * float(dt_ms) / 1000.0
            for combatant_id in self._threat:
                self._threat[combatant_id] = max(
                    0.0, self._threat[combatant_id] - decay
                )

    def set_paused(self, paused):
        with self._lock:
            self._paused = bool(paused)

    def change_scene(self):
        with self._lock:
            if self._pending:
                self._next_seq = max(self._next_seq, max(self._pending) + 1)
            self._pending.clear()
            self._combatants.clear()
            self._threat.clear()
            self._taunt = None
            self._vulnerable = True
            self._paused = False
            self._join_order = 0

    def wipe(self):
        with self._lock:
            for combatant_id in self._threat:
                self._threat[combatant_id] = 0.0
            for combatant in self._combatants.values():
                combatant["dead"] = False
                combatant["stealth"] = False
            self._taunt = None

    def snapshot(self):
        with self._lock:
            combatants = [
                {"id": combatant_id, **combatant}
                for combatant_id, combatant in self._combatants.items()
            ]
            return copy.deepcopy({
                "version": 1,
                "heal_multiplier": self.heal_multiplier,
                "stealth_multiplier": self.stealth_multiplier,
                "decay_per_second": self.decay_per_second,
                "combatants": combatants,
                "threat": self._threat,
                "applied": sorted(self._applied),
                "next_seq": self._next_seq,
                "pending": list(self._pending.values()),
                "taunt": self._taunt,
                "vulnerable": self._vulnerable,
                "paused": self._paused,
                "join_order": self._join_order,
            })

    def restore(self, snapshot):
        with self._lock:
            data = copy.deepcopy(dict(snapshot))
            self.heal_multiplier = float(
                data.get("heal_multiplier", self.heal_multiplier)
            )
            self.stealth_multiplier = float(
                data.get("stealth_multiplier", self.stealth_multiplier)
            )
            self.decay_per_second = float(
                data.get("decay_per_second", self.decay_per_second)
            )
            self._combatants = {}
            self._threat = {}
            for combatant in data.get("combatants", []):
                combatant_id = combatant["id"]
                self._combatants[combatant_id] = {
                    "role": combatant.get("role", "dps"),
                    "joined_at": combatant.get("joined_at", 0),
                    "owner": combatant.get("owner"),
                    "dead": bool(combatant.get("dead", False)),
                    "stealth": bool(combatant.get("stealth", False)),
                    "order": combatant.get("order", 0),
                    "parent": combatant.get("parent"),
                }
            for combatant_id, value in data.get("threat", {}).items():
                self._threat[combatant_id] = float(value)
            for combatant_id in self._combatants:
                self._threat.setdefault(combatant_id, 0.0)

            self._applied = {int(seq) for seq in data.get("applied", [])}
            self._next_seq = int(data.get("next_seq", 1))
            self._pending = {
                int(event["seq"]): event
                for event in data.get("pending", [])
            }
            taunt = data.get("taunt")
            self._taunt = tuple(taunt) if taunt is not None else None
            self._vulnerable = bool(data.get("vulnerable", True))
            self._paused = bool(data.get("paused", False))
            self._join_order = int(data.get("join_order", max(
                (combatant["order"] for combatant in
                 self._combatants.values()),
                default=0,
            )))

    def _drain_pending(self):
        applied = 0
        while self._next_seq in self._pending:
            event = self._pending.pop(self._next_seq)
            if self._apply_event(event):
                applied += 1
            self._applied.add(self._next_seq)
            self._next_seq += 1
        return applied

    def _validate_event(self, event):
        seq = event.get("seq")
        if isinstance(seq, bool) or not isinstance(seq, int) or seq < 1:
            raise ValueError("event seq must be a positive integer")

        event_type = event.get("type")
        if event_type == "damage":
            self._event_combatant(event)
            self._event_amount(event)
        elif event_type == "heal":
            self._event_combatant(event)
            self._event_amount(event)
        elif event_type == "taunt":
            self._event_combatant(event)
            self._event_until(event)
        elif event_type == "stealth":
            self._event_combatant(event)
        elif event_type == "death":
            self._event_combatant(event)
        else:
            raise ValueError("unknown event type: {!r}".format(event_type))
        return seq

    def _apply_event(self, event):
        event_type = event["type"]
        if event_type == "damage":
            recipient = self._threat_owner(self._event_combatant(event))
            self._threat[recipient] += self._event_amount(event)
            return True
        if event_type == "heal":
            recipient = self._threat_owner(self._event_combatant(event))
            amount = self._event_amount(event)
            if not self._vulnerable:
                return False
            self._threat[recipient] += amount * self.heal_multiplier
            return True
        if event_type == "taunt":
            self._set_taunt(
                self._event_combatant(event),
                self._event_until(event),
            )
            return True
        if event_type == "stealth":
            combatant_id = self._event_combatant(event)
            active = event.get(
                "stealth", event.get("active", event.get("value", True))
            )
            self._combatants[combatant_id]["stealth"] = bool(active)
            return True
        if event_type == "death":
            self._kill(self._event_combatant(event))
            return True
        raise ValueError("unknown event type: {!r}".format(event_type))

    def _event_combatant(self, event):
        for key in ("source", "combatant_id", "target"):
            if key in event:
                combatant_id = event[key]
                self._require_combatant(combatant_id)
                return combatant_id
        raise ValueError("event has no combatant")

    def _event_amount(self, event):
        amount = event.get("amount")
        if isinstance(amount, bool) or not isinstance(amount, (int, float)):
            raise ValueError("event amount must be numeric")
        if amount < 0:
            raise ValueError("event amount must not be negative")
        return float(amount)

    def _event_until(self, event):
        until_ms = event.get("until_ms")
        if isinstance(until_ms, bool) or not isinstance(until_ms, (int, float)):
            raise ValueError("taunt until_ms must be numeric")
        return until_ms

    def _set_taunt(self, combatant_id, until_ms):
        if isinstance(until_ms, bool) or not isinstance(until_ms, (int, float)):
            raise ValueError("taunt until_ms must be numeric")
        self._taunt = (combatant_id, until_ms)

    def _threat_owner(self, combatant_id):
        owner = self._combatants[combatant_id]["owner"]
        if owner is None:
            return combatant_id
        self._require_combatant(owner)
        return owner

    def _kill(self, combatant_id):
        self._combatants[combatant_id]["dead"] = True
        self._threat[combatant_id] = 0.0
        if self._taunt is not None and self._taunt[0] == combatant_id:
            self._taunt = None
        for other_id, combatant in self._combatants.items():
            if combatant["owner"] == combatant_id:
                combatant["dead"] = True
                self._threat[other_id] = 0.0
                if self._taunt is not None and self._taunt[0] == other_id:
                    self._taunt = None

    def _require_combatant(self, combatant_id):
        try:
            return self._combatants[combatant_id]
        except KeyError:
            raise UnknownCombatantError(combatant_id) from None
