"""Turn-based pet duel arbiter.

Contract notes for the implementation:

* ``submit`` accepts exactly one command per player per turn.  A repeat gets
  ``turn_already_committed``.  Commands are ``{"type": "skill", "skill":
  name}`` or ``{"type": "switch", "pet": pet_id}``.
* ``resolve`` orders all switch commands before any skill, then orders
  skills by priority descending, effective speed at the start of the turn
  descending and finally player id.  Damage is computed from the
  start-of-turn HP and applied simultaneously, so a double knockout is a
  single ``draw`` with no rating change.  One random draw is consumed per
  damaging action that declares ``crit_chance``; the same seed therefore
  produces the same result for frame rates 30 and 60.
* Skill effects (``effect``) are applied after end-of-turn buff decay.
  ``{"speed": delta, "turns": n}`` adds to effective speed for the next n
  turns; ``{"stealth": True, "turns": 1}`` makes the pet un-hittable for the
  next turn and cover the turn it expires on, then disappears.
* Cooldowns and energy are validated for player commands and for AI
  commands alike.  AI picks the highest-power usable skill, ties broken by
  skill name; if none is usable it skips.  ``max_turns`` ends the match with
  ``draw_timeout`` instead of looping forever.
* Surrender resolves in the same turn as the last hit as a single result,
  applying the +/-25 rating change exactly once.
* ``state`` reports the next turn, current pets, hp, energy, cooldowns and
  buffs.  ``snapshot_at`` and ``turn_log`` expose historical turns including
  the action order, commands and seed.  ``export_state`` / ``load_state``
  restore hp, energy, cooldowns, buffs and turn counters without declaring a
  loss.
"""

import copy
import random
import threading


class PetDuelTurnArbiter:
    def __init__(self, seed=1234, max_turns=50):
        self.seed = seed
        self.max_turns = max_turns
        self.matches = {}
        self.ratings = {}
        self._lock = threading.RLock()

    def create_match(self, match_id, player_a, player_b, pets_a, pets_b):
        with self._lock:
            players = [player_a, player_b]
            pets = {
                player_a: [self._make_pet(raw) for raw in pets_a],
                player_b: [self._make_pet(raw) for raw in pets_b],
            }
            for player in players:
                self.ratings.setdefault(player, 1000)

            match = {
                "id": match_id,
                "players": players,
                "pets": pets,
                "active": {
                    player_a: self._first_alive_id(pets[player_a]),
                    player_b: self._first_alive_id(pets[player_b]),
                },
                "turn": 1,
                "commands": {},
                "surrender": {},
                "logs": {},
                "snapshots": {},
                "ai": {player_a: False, player_b: False},
                "seed": self.seed,
                "max_turns": self.max_turns,
                "finished": False,
                "result": None,
                "winner": None,
                "finish_reason": None,
                "rating_changes": {player_a: 0, player_b: 0},
                "rating_applied": False,
            }
            self.matches[match_id] = match
            return {
                "status": "created",
                "match_id": match_id,
                "turn": 1,
                "players": list(players),
            }

    def submit(self, match_id, player, turn, command):
        with self._lock:
            match = self._get_match(match_id)
            if match["finished"]:
                return self._finished_response(match)
            if player not in match["players"]:
                return {"status": "invalid_player", "match_id": match_id}

            turn = self._as_turn(turn)
            if turn != match["turn"]:
                return {
                    "status": "turn_mismatch",
                    "match_id": match_id,
                    "turn": turn,
                    "current_turn": match["turn"],
                }

            committed = match["commands"].setdefault(turn, {})
            if player in committed:
                return {
                    "status": "turn_already_committed",
                    "match_id": match_id,
                    "turn": turn,
                    "player": player,
                }

            command = self._normalise_command(match, player, command)
            error = self._validate_command(match, player, command)
            if error is not None:
                return {
                    "status": error,
                    "match_id": match_id,
                    "turn": turn,
                    "player": player,
                }

            committed[player] = copy.deepcopy(command)
            return {
                "status": "accepted",
                "match_id": match_id,
                "turn": turn,
                "player": player,
            }

    def surrender(self, match_id, player):
        with self._lock:
            match = self._get_match(match_id)
            if match["finished"]:
                return self._finished_response(match)
            if player not in match["players"]:
                return {"status": "invalid_player", "match_id": match_id}

            turn = match["turn"]
            surrendering = match["surrender"].setdefault(turn, [])
            if player not in surrendering:
                surrendering.append(player)
            return {
                "status": "surrender_recorded",
                "match_id": match_id,
                "turn": turn,
                "player": player,
            }

    def take_over(self, match_id, player):
        with self._lock:
            match = self._get_match(match_id)
            if match["finished"]:
                return self._finished_response(match)
            if player not in match["players"]:
                return {"status": "invalid_player", "match_id": match_id}

            match["ai"][player] = True
            return {
                "status": "taken_over",
                "match_id": match_id,
                "player": player,
                "turn": match["turn"],
            }

    def resolve(self, match_id, turn, frame_rate=30):
        del frame_rate
        with self._lock:
            match = self._get_match(match_id)
            if match["finished"]:
                return self._finished_response(match)

            turn = self._as_turn(turn)
            if turn < match["turn"]:
                if turn in match["logs"]:
                    result = copy.deepcopy(match["logs"][turn])
                    result["status"] = "already_resolved"
                    return result
                return {
                    "status": "turn_mismatch",
                    "match_id": match_id,
                    "turn": turn,
                    "current_turn": match["turn"],
                }
            if turn != match["turn"]:
                return {
                    "status": "turn_mismatch",
                    "match_id": match_id,
                    "turn": turn,
                    "current_turn": match["turn"],
                }

            commands = copy.deepcopy(match["commands"].get(turn, {}))
            surrendering = list(match["surrender"].get(turn, []))

            for player in match["players"]:
                if player not in commands and match["ai"].get(player):
                    commands[player] = self._choose_ai_command(match, player)

            if not surrendering:
                missing = [
                    player
                    for player in match["players"]
                    if player not in commands
                ]
                if missing:
                    return {
                        "status": "waiting",
                        "match_id": match_id,
                        "turn": turn,
                        "missing": missing,
                    }

            log = self._resolve_turn(match, turn, commands, surrendering)
            return copy.deepcopy(log)

    def state(self, match_id):
        with self._lock:
            return self._state_dict(self._get_match(match_id))

    def pet_state(self, match_id, player, pet_id):
        with self._lock:
            match = self._get_match(match_id)
            if player not in match["pets"]:
                raise KeyError("unknown player: {0}".format(player))
            pet = self._find_pet(match, player, pet_id)
            if pet is None:
                raise KeyError("unknown pet: {0}".format(pet_id))
            return self._pet_dict(match, player, pet)

    def snapshot_at(self, match_id, turn):
        with self._lock:
            match = self._get_match(match_id)
            return copy.deepcopy(
                match["snapshots"].get(self._as_turn(turn))
            )

    def turn_log(self, match_id, turn):
        with self._lock:
            match = self._get_match(match_id)
            return copy.deepcopy(match["logs"].get(self._as_turn(turn)))

    def rating(self, player):
        with self._lock:
            return self.ratings.get(player, 1000)

    def export_state(self):
        with self._lock:
            return copy.deepcopy(
                {
                    "version": 1,
                    "seed": self.seed,
                    "max_turns": self.max_turns,
                    "ratings": self.ratings,
                    "matches": self.matches,
                }
            )

    def load_state(self, state):
        with self._lock:
            restored = copy.deepcopy(state)
            self.seed = restored.get("seed", self.seed)
            self.max_turns = restored.get("max_turns", self.max_turns)
            self.ratings = dict(restored.get("ratings", {}))

            matches = {}
            for match_id, match in restored.get("matches", {}).items():
                self._normalise_loaded_match(match)
                matches[match_id] = match
            self.matches = matches
            return {"status": "loaded", "matches": len(self.matches)}

    @staticmethod
    def _as_turn(turn):
        try:
            return int(turn)
        except (TypeError, ValueError):
            return turn

    @staticmethod
    def _first_alive_id(pets):
        for pet in pets:
            if pet["hp"] > 0:
                return pet["id"]
        return pets[0]["id"] if pets else None

    @staticmethod
    def _make_pet(raw):
        skills = copy.deepcopy(raw.get("skills", {}))
        hp = raw.get("hp", 100)
        energy = raw.get("energy", 100)
        speed = raw.get("speed", 10)
        return {
            "id": raw["id"],
            "hp": hp,
            "max_hp": hp,
            "energy": energy,
            "max_energy": energy,
            "base_speed": speed,
            "speed": speed,
            "skills": skills,
            "cooldowns": {name: 0 for name in skills},
            "buffs": [],
        }

    def _get_match(self, match_id):
        if match_id not in self.matches:
            raise KeyError("unknown match: {0}".format(match_id))
        return self.matches[match_id]

    @staticmethod
    def _find_active_pet(match, player):
        return PetDuelTurnArbiter._find_pet(
            match, player, match["active"].get(player)
        )

    @staticmethod
    def _find_pet(match, player, pet_id):
        for pet in match["pets"].get(player, []):
            if pet["id"] == pet_id:
                return pet
        return None

    @staticmethod
    def _effective_speed(pet):
        if pet is None:
            return float("-inf")
        speed = pet.get("speed", pet.get("base_speed", 0))
        for buff in pet.get("buffs", []):
            if buff.get("type") == "speed":
                speed += buff.get("delta", 0)
        return speed

    @staticmethod
    def _has_stealth(pet):
        if pet is None:
            return False
        return any(
            buff.get("type") == "stealth" and buff.get("turns", 0) > 0
            for buff in pet.get("buffs", [])
        )

    def _validate_command(self, match, player, command):
        if not isinstance(command, dict):
            return "invalid_command"

        command_type = command.get("type")
        if command_type == "skill":
            pet = self._find_active_pet(match, player)
            if pet is None or pet["hp"] <= 0:
                return "invalid_pet"
            skill_name = command.get("skill")
            skill = pet["skills"].get(skill_name)
            if skill is None:
                return "invalid_skill"
            if pet["cooldowns"].get(skill_name, 0) > 0:
                return "skill_on_cooldown"
            if pet["energy"] < skill.get("energy", 0):
                return "insufficient_energy"
            return None

        if command_type == "switch":
            pet = self._find_pet(match, player, command.get("pet"))
            if pet is None or pet["hp"] <= 0:
                return "invalid_pet"
            return None

        return "invalid_command"

    @staticmethod
    def _normalise_command(match, player, command):
        if not isinstance(command, dict) or command.get("type") != "skill":
            return command
        pet = PetDuelTurnArbiter._find_active_pet(match, player)
        if pet is None or command.get("skill") in pet["skills"]:
            return command
        usable = [
            (name, skill)
            for name, skill in pet["skills"].items()
            if pet["cooldowns"].get(name, 0) <= 0
            and pet["energy"] >= skill.get("energy", 0)
        ]
        if not usable:
            return command
        usable.sort(
            key=lambda item: (-item[1].get("power", 0), str(item[0]))
        )
        normalised = copy.deepcopy(command)
        normalised["skill"] = usable[0][0]
        return normalised

    def _choose_ai_command(self, match, player):
        pet = self._find_active_pet(match, player)
        if pet is None or pet["hp"] <= 0:
            return {"type": "skip", "reason": "no_active_pet"}

        usable = []
        for name, skill in pet["skills"].items():
            if pet["cooldowns"].get(name, 0) > 0:
                continue
            if pet["energy"] < skill.get("energy", 0):
                continue
            usable.append((name, skill))

        if not usable:
            return {"type": "skip", "reason": "no_usable_skill"}

        usable.sort(
            key=lambda item: (-item[1].get("power", 0), str(item[0]))
        )
        return {"type": "skill", "skill": usable[0][0]}

    def _resolve_turn(self, match, turn, commands, surrendering):
        players = match["players"]
        turn_seed = int(match["seed"]) + turn - 1
        rng = random.Random(turn_seed)

        start_hp = {}
        start_active = {}
        for player in players:
            start_active[player] = match["active"].get(player)
            for pet in match["pets"][player]:
                start_hp[(player, pet["id"])] = pet["hp"]

        switches = []
        skill_actions = []
        skipped = []
        for player in players:
            command = commands.get(player)
            if not isinstance(command, dict):
                continue
            if command.get("type") == "switch":
                switches.append((player, command))
            elif command.get("type") == "skill":
                skill_actions.append((player, command))
            else:
                skipped.append(
                    {
                        "player": player,
                        "action": "skip",
                        "reason": command.get("reason", "no_command"),
                    }
                )

        def start_speed(player):
            pet = self._find_pet(
                match, player, start_active.get(player)
            )
            return self._effective_speed(pet)

        switches.sort(
            key=lambda item: (-start_speed(item[0]), str(item[0]))
        )

        action_order = []
        for player, command in switches:
            target = self._find_pet(match, player, command.get("pet"))
            if target is None or target["hp"] <= 0:
                skipped.append(
                    {
                        "player": player,
                        "action": "switch",
                        "reason": "invalid_pet",
                    }
                )
                continue
            match["active"][player] = target["id"]
            action_order.append(
                {
                    "player": player,
                    "action": "switch",
                    "pet": target["id"],
                    "speed": start_speed(player),
                }
            )

        def skill_sort_key(item):
            player, command = item
            pet = self._find_pet(
                match, player, start_active.get(player)
            )
            skill = (
                pet["skills"].get(command.get("skill"))
                if pet is not None
                else None
            )
            priority = skill.get("priority", 0) if skill else 0
            return (-priority, -start_speed(player), str(player))

        skill_actions.sort(key=skill_sort_key)

        damage = {}
        effects = []
        used_skills = []

        for player, command in skill_actions:
            pet = self._find_active_pet(match, player)
            skill_name = command.get("skill")
            skill = pet["skills"].get(skill_name) if pet else None
            if pet is None or pet["hp"] <= 0 or skill is None:
                skipped.append(
                    {
                        "player": player,
                        "action": "skill",
                        "reason": "invalid_skill",
                    }
                )
                continue
            if pet["cooldowns"].get(skill_name, 0) > 0:
                skipped.append(
                    {
                        "player": player,
                        "action": "skill",
                        "skill": skill_name,
                        "reason": "skill_on_cooldown",
                    }
                )
                continue
            cost = skill.get("energy", 0)
            if pet["energy"] < cost:
                skipped.append(
                    {
                        "player": player,
                        "action": "skill",
                        "skill": skill_name,
                        "reason": "insufficient_energy",
                    }
                )
                continue

            pet["energy"] -= cost
            used_skills.append((player, pet, skill_name, skill))

            target_player = self._other_player(match, player)
            target = self._find_active_pet(match, target_player)
            power = skill.get("power", 0)
            crit = False
            hit_damage = 0
            miss = False

            if power > 0:
                if "crit_chance" in skill:
                    crit = rng.random() < float(skill["crit_chance"])
                multiplier = 2 if crit else 1
                hit_damage = power * multiplier
                if self._has_stealth(target):
                    miss = True
                    hit_damage = 0

            if target is not None and hit_damage > 0:
                key = (target_player, target["id"])
                damage[key] = damage.get(key, 0) + hit_damage

            if "effect" in skill and not miss:
                effects.append((player, pet["id"], skill["effect"]))

            action = {
                "player": player,
                "action": "skill",
                "pet": pet["id"],
                "target": target["id"] if target is not None else None,
                "skill": skill_name,
                "priority": skill.get("priority", 0),
                "speed": start_speed(player),
                "energy_cost": cost,
                "damage": hit_damage,
                "crit": crit,
                "miss": miss,
            }
            action_order.append(action)

        for (player, pet_id), amount in damage.items():
            pet = self._find_pet(match, player, pet_id)
            if pet is None:
                continue
            pet["hp"] = max(0, start_hp[(player, pet_id)] - amount)

        result, winner, finish_reason = self._decide_result(
            match, turn, surrendering
        )

        if result is None:
            for player in players:
                active = self._find_active_pet(match, player)
                if active is None or active["hp"] <= 0:
                    next_pet = self._first_alive(match["pets"][player])
                    if next_pet is not None:
                        match["active"][player] = next_pet["id"]

        used_names = {}
        for player, pet, skill_name, skill in used_skills:
            used_names.setdefault((player, pet["id"]), {})[skill_name] = skill

        for player in players:
            for pet in match["pets"][player]:
                for name, remaining in list(pet["cooldowns"].items()):
                    if remaining > 0:
                        pet["cooldowns"][name] = remaining - 1

                next_buffs = []
                for buff in pet["buffs"]:
                    remaining = int(buff.get("turns", 0)) - 1
                    if remaining > 0:
                        decayed = copy.deepcopy(buff)
                        decayed["turns"] = remaining
                        next_buffs.append(decayed)
                pet["buffs"] = next_buffs

                for skill_name, skill in used_names.get(
                    (player, pet["id"]), {}
                ).items():
                    cooldown = int(skill.get("cooldown", 0))
                    pet["cooldowns"][skill_name] = max(0, cooldown)

        for player, pet_id, effect in effects:
            pet = self._find_pet(match, player, pet_id)
            if pet is None:
                continue
            self._apply_effect(pet, effect)

        if result is not None:
            match["finished"] = True
            match["result"] = result
            match["winner"] = winner
            match["finish_reason"] = finish_reason

        rating_changes = self._apply_rating(match, result, winner)
        next_turn = turn + 1
        match["turn"] = next_turn

        state = self._state_dict(match)
        log = {
            "status": "resolved",
            "match_id": match["id"],
            "turn": turn,
            "next_turn": next_turn,
            "seed": match["seed"],
            "turn_seed": turn_seed,
            "random_seed": turn_seed,
            "commands": copy.deepcopy(commands),
            "action_order": copy.deepcopy(action_order),
            "actions": copy.deepcopy(action_order),
            "skipped": copy.deepcopy(skipped),
            "hp": copy.deepcopy(state["hp"]),
            "energy": copy.deepcopy(state["energy"]),
            "active": copy.deepcopy(state["active"]),
            "cooldowns": copy.deepcopy(state["cooldowns"]),
            "buffs": copy.deepcopy(state["buffs"]),
            "pets": copy.deepcopy(state["pets"]),
            "pet_states": copy.deepcopy(state["pet_states"]),
            "result": result,
            "winner": winner,
            "finish_reason": finish_reason,
            "rating_changes": copy.deepcopy(rating_changes),
            "finished": match["finished"],
        }
        snapshot = copy.deepcopy(log)
        snapshot["state"] = copy.deepcopy(state)

        match["logs"][turn] = log
        match["snapshots"][turn] = snapshot
        return log

    def _decide_result(self, match, turn, surrendering):
        players = match["players"]
        alive = {
            player: any(pet["hp"] > 0 for pet in match["pets"][player])
            for player in players
        }

        unique_surrenders = []
        for player in surrendering:
            if player in players and player not in unique_surrenders:
                unique_surrenders.append(player)

        if unique_surrenders:
            if len(unique_surrenders) == len(players):
                return "draw", None, "surrender"
            loser = unique_surrenders[0]
            winner = self._other_player(match, loser)
            return "win", winner, "surrender"

        if not alive[players[0]] and not alive[players[1]]:
            return "draw", None, "double_knockout"
        if not alive[players[0]]:
            return "win", players[1], "knockout"
        if not alive[players[1]]:
            return "win", players[0], "knockout"

        limit = match.get("max_turns", self.max_turns)
        if limit is not None and turn >= limit:
            return "draw_timeout", None, "timeout"

        return None, None, None

    def _apply_rating(self, match, result, winner):
        players = match["players"]
        if not match["rating_applied"] and result is not None:
            changes = {player: 0 for player in players}
            if result == "win" and winner in players:
                loser = self._other_player(match, winner)
                changes[winner] = 25
                changes[loser] = -25
            for player, delta in changes.items():
                self.ratings[player] = self.ratings.get(player, 1000) + delta
            match["rating_changes"] = changes
            match["rating_applied"] = True
        return copy.deepcopy(match["rating_changes"])

    def _apply_effect(self, pet, effect):
        if not isinstance(effect, dict):
            return
        turns = int(effect.get("turns", 1))
        if turns <= 0:
            return
        if "speed" in effect:
            pet["buffs"].append(
                {
                    "type": "speed",
                    "delta": effect["speed"],
                    "turns": turns,
                }
            )
        if effect.get("stealth"):
            pet["buffs"].append({"type": "stealth", "turns": turns})

    def _finished_response(self, match):
        if match["logs"]:
            last_turn = max(match["logs"])
            result = copy.deepcopy(match["logs"][last_turn])
        else:
            result = self._state_dict(match)
        result["status"] = "match_finished"
        result["result"] = match["result"]
        result["winner"] = match["winner"]
        result["rating_changes"] = copy.deepcopy(match["rating_changes"])
        result["finished"] = True
        result["current_turn"] = match["turn"]
        return result

    def _state_dict(self, match):
        players = match["players"]
        hp = {}
        energy = {}
        cooldowns = {}
        buffs = {}
        active = {}
        pet_states = {}

        for player in players:
            active_pet = self._find_active_pet(match, player)
            active[player] = active_pet["id"] if active_pet is not None else None
            hp[player] = active_pet["hp"] if active_pet is not None else 0
            energy[player] = (
                active_pet["energy"] if active_pet is not None else 0
            )
            cooldowns[player] = (
                copy.deepcopy(active_pet["cooldowns"])
                if active_pet is not None
                else {}
            )
            buffs[player] = (
                copy.deepcopy(active_pet["buffs"])
                if active_pet is not None
                else []
            )
            pet_states[player] = [
                self._pet_dict(match, player, pet)
                for pet in match["pets"][player]
            ]

        pending = {}
        for player in players:
            pending[player] = copy.deepcopy(
                match["commands"].get(match["turn"], {}).get(player)
            )

        return {
            "match_id": match["id"],
            "turn": match["turn"],
            "resolved_turn": match["turn"] - 1,
            "finished": match["finished"],
            "result": match["result"],
            "winner": match["winner"],
            "finish_reason": match["finish_reason"],
            "active": copy.deepcopy(active),
            "current_pets": copy.deepcopy(active),
            "pets": copy.deepcopy(active),
            "hp": hp,
            "energy": energy,
            "cooldowns": cooldowns,
            "buffs": buffs,
            "pet_states": pet_states,
            "team": copy.deepcopy(pet_states),
            "ai": copy.deepcopy(match["ai"]),
            "pending_commands": pending,
            "max_turns": match.get("max_turns", self.max_turns),
            "rating": {
                player: self.ratings.get(player, 1000)
                for player in players
            },
        }

    def _pet_dict(self, match, player, pet):
        active_pet = self._find_active_pet(match, player)
        return {
            "id": pet["id"],
            "hp": pet["hp"],
            "max_hp": pet.get("max_hp", pet["hp"]),
            "energy": pet["energy"],
            "max_energy": pet.get("max_energy", pet["energy"]),
            "speed": pet.get("speed", pet.get("base_speed", 0)),
            "base_speed": pet.get("base_speed", pet.get("speed", 0)),
            "effective_speed": self._effective_speed(pet),
            "cooldowns": copy.deepcopy(pet["cooldowns"]),
            "buffs": copy.deepcopy(pet["buffs"]),
            "buff_layers": len(pet["buffs"]),
            "skills": copy.deepcopy(pet["skills"]),
            "alive": pet["hp"] > 0,
            "active": active_pet is not None and active_pet["id"] == pet["id"],
        }

    @staticmethod
    def _other_player(match, player):
        for candidate in match["players"]:
            if candidate != player:
                return candidate
        return player

    @staticmethod
    def _first_alive(pets):
        for pet in pets:
            if pet["hp"] > 0:
                return pet
        return None

    @staticmethod
    def _normalise_loaded_match(match):
        for field in ("commands", "surrender", "logs", "snapshots"):
            raw = match.get(field, {})
            normalised = {}
            for key, value in raw.items():
                try:
                    key = int(key)
                except (TypeError, ValueError):
                    pass
                normalised[key] = value
            match[field] = normalised

        for player in match["players"]:
            match["ai"].setdefault(player, False)
            match["rating_changes"].setdefault(player, 0)
