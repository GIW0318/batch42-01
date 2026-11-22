"""Companion command queue, follow/attack and scene migration."""

import copy
import threading


ALREADY_TAMED = "already_tamed"
OLD_SEQUENCE = "old_sequence"
MIGRATION_FAILED = "migration_failed"
ALREADY_ALIVE = "already_alive"
OWNER_DEAD = "owner_dead"
ALREADY_SUMMONED = "already_summoned"
UNKNOWN_PET = "unknown_pet"
STUNNED = "stunned"
PET_DEAD = "pet_dead"


class CompanionError(Exception):
    def __init__(self, code, message=""):
        self.code = code
        super().__init__(code if not message else "%s: %s" % (code, message))


class CompanionAiCommand:
    def __init__(self, server_time=0, migration_hook=None):
        self.server_time = server_time
        self.migration_hook = migration_hook
        self._lock = threading.RLock()
        self._pets = {}
        self._owner_sequences = {}
        self._creature_owners = {}
        self._owner_scenes = {}
        self._owner_alive = {}
        self._storage = {}
        self._quest_logs = {}

    def _pet(self, pet_id):
        pet = self._pets.get(pet_id)
        if pet is None:
            raise CompanionError(UNKNOWN_PET, pet_id)
        return pet

    def _append_to_storage(self, owner_id, items):
        if items:
            self._storage.setdefault(owner_id, []).extend(items)

    def _apply_command(self, pet, entry):
        command = entry["command"]
        pet["command"] = command
        pet["target"] = entry.get("target")
        if command == "attack":
            pet["status"] = "attacking"
        elif command in ("follow", "recall"):
            pet["command"] = "follow"
            pet["status"] = "following"
        else:
            pet["status"] = "idle"

    def advance(self, seconds):
        with self._lock:
            self.server_time += seconds
            return self.server_time

    def register_pet(self, pet_id, owner_id, scene_id="scene-1", name=None):
        with self._lock:
            if pet_id in self._pets:
                raise CompanionError("already_registered", pet_id)
            self._pets[pet_id] = {
                "pet_id": pet_id,
                "owner_id": owner_id,
                "scene_id": scene_id,
                "name": name,
                "status": "idle",
                "command": "follow",
                "target": None,
                "position": None,
                "skills": [],
                "buffs": [],
                "bag": [],
                "queue": [],
                "stunned": False,
                "summoned": True,
            }
            self._owner_scenes.setdefault(owner_id, scene_id)
            self._owner_alive.setdefault(owner_id, True)
            self._storage.setdefault(owner_id, [])
            self._quest_logs.setdefault(owner_id, [])
            return copy.deepcopy(self._pets[pet_id])

    def tame(self, creature_id, player_id):
        with self._lock:
            if creature_id in self._creature_owners:
                raise CompanionError(ALREADY_TAMED, creature_id)
            self._creature_owners[creature_id] = player_id
            return {
                "status": "tamed",
                "creature_id": creature_id,
                "owner_id": player_id,
            }

    def creature_owner(self, creature_id):
        with self._lock:
            return self._creature_owners.get(creature_id)

    def issue_command(self, pet_id, seq, command, target=None):
        with self._lock:
            pet = self._pet(pet_id)
            owner_id = pet["owner_id"]
            last_seq = self._owner_sequences.get(owner_id)
            if last_seq is not None and seq <= last_seq:
                return OLD_SEQUENCE
            if not self._owner_alive.get(owner_id, True):
                raise CompanionError(OWNER_DEAD, owner_id)

            self._owner_sequences[owner_id] = seq
            if (
                not pet["summoned"]
                or pet["status"] in ("dead", "despawned", "dismissed")
            ):
                return None

            entry = {"seq": seq, "command": command, "target": target}
            pet["queue"].append(entry)
            pet["queue"].sort(key=lambda item: item["seq"])
            return copy.deepcopy(entry)

    def pending_commands(self, pet_id):
        with self._lock:
            pet = self._pet(pet_id)
            return copy.deepcopy(pet["queue"])

    def execute_next(self, pet_id):
        with self._lock:
            pet = self._pet(pet_id)
            if pet["stunned"]:
                raise CompanionError(STUNNED, pet_id)
            if (
                not pet["summoned"]
                or pet["status"] in ("dead", "despawned", "dismissed")
            ):
                return None
            if not pet["queue"]:
                return None

            entry = pet["queue"].pop(0)
            self._apply_command(pet, entry)
            return copy.deepcopy(entry)

    def follow(self, pet_id):
        with self._lock:
            pet = self._pet(pet_id)
            pet["queue"].clear()
            pet["command"] = "follow"
            pet["target"] = None
            pet["status"] = "following"
            return copy.deepcopy(pet)

    def recall(self, pet_id):
        with self._lock:
            pet = self._pet(pet_id)
            pet["queue"].clear()
            pet["command"] = "follow"
            pet["target"] = None
            pet["status"] = "idle"
            return copy.deepcopy(pet)

    def migrate(self, pet_id, scene_id):
        with self._lock:
            pet = self._pet(pet_id)
            from_scene = pet["scene_id"]
            if from_scene == scene_id:
                return copy.deepcopy(pet)

            owner_id = pet["owner_id"]
            old_owner_scene = self._owner_scenes.get(owner_id)
            try:
                if self.migration_hook is not None:
                    result = self.migration_hook(pet_id, from_scene, scene_id)
                    if result is False:
                        raise RuntimeError("scene transfer rejected")

                pet["scene_id"] = scene_id
                self._owner_scenes[owner_id] = scene_id
                pet["buffs"].clear()
            except Exception as exc:
                pet["scene_id"] = from_scene
                if old_owner_scene is None:
                    self._owner_scenes.pop(owner_id, None)
                else:
                    self._owner_scenes[owner_id] = old_owner_scene
                raise CompanionError(MIGRATION_FAILED, str(exc)) from exc

            return copy.deepcopy(pet)

    def pet_died(self, pet_id):
        with self._lock:
            pet = self._pet(pet_id)
            pet["queue"].clear()
            pet["target"] = None
            pet["command"] = "idle"
            pet["status"] = "dead"
            pet["summoned"] = False
            pet["stunned"] = False
            return copy.deepcopy(pet)

    def revive(self, pet_id):
        with self._lock:
            pet = self._pet(pet_id)
            if pet["status"] != "dead":
                raise CompanionError(ALREADY_ALIVE, pet_id)
            if not self._owner_alive.get(pet["owner_id"], True):
                raise CompanionError(OWNER_DEAD, pet["owner_id"])

            pet["queue"].clear()
            pet["target"] = None
            pet["command"] = "follow"
            pet["status"] = "idle"
            pet["summoned"] = True
            pet["stunned"] = False
            return {"status": "revived", "pet_id": pet_id}

    def owner_died(self, player_id):
        with self._lock:
            self._owner_alive[player_id] = False
            for pet in self._pets.values():
                if pet["owner_id"] != player_id:
                    continue
                pet["queue"].clear()
                pet["target"] = None
                pet["command"] = "idle"
                if pet["status"] not in ("dead", "dismissed"):
                    pet["status"] = "despawned"
                pet["summoned"] = False
                pet["stunned"] = False
            return {"status": "owner_dead", "owner_id": player_id}

    def revive_owner(self, player_id):
        with self._lock:
            self._owner_alive[player_id] = True
            return {"status": "owner_revived", "owner_id": player_id}

    def summon(self, pet_id):
        with self._lock:
            pet = self._pet(pet_id)
            owner_id = pet["owner_id"]
            if not self._owner_alive.get(owner_id, True):
                raise CompanionError(OWNER_DEAD, owner_id)
            if pet["status"] == "dead":
                raise CompanionError(PET_DEAD, pet_id)
            if pet["summoned"]:
                raise CompanionError(ALREADY_SUMMONED, pet_id)

            restored = self._storage.get(owner_id, [])
            if restored:
                pet["bag"].extend(restored)
                restored.clear()
            pet["summoned"] = True
            pet["status"] = "summoned"
            pet["command"] = "follow"
            pet["target"] = None
            return {"status": "summoned", "pet_id": pet_id}

    def dismiss(self, pet_id):
        with self._lock:
            pet = self._pet(pet_id)
            owner_id = pet["owner_id"]
            self._append_to_storage(owner_id, pet["bag"])
            pet["bag"].clear()
            pet["queue"].clear()
            pet["target"] = None
            pet["command"] = "idle"
            pet["status"] = "dismissed"
            pet["summoned"] = False
            pet["stunned"] = False
            return {"status": "dismissed", "pet_id": pet_id}

    def rename_pet(self, pet_id, name):
        with self._lock:
            pet = self._pet(pet_id)
            pet["name"] = name
            return copy.deepcopy(pet)

    def auto_pickup(self, pet_id, item_id, quest_item=False):
        with self._lock:
            pet = self._pet(pet_id)
            owner_id = pet["owner_id"]
            if quest_item:
                self._quest_logs.setdefault(owner_id, []).append(item_id)
                destination = "quest_log"
            else:
                pet["bag"].append(item_id)
                destination = "companion_bag"
            return {
                "status": "picked_up",
                "item_id": item_id,
                "destination": destination,
            }

    def add_bag_item(self, pet_id, item_id):
        with self._lock:
            pet = self._pet(pet_id)
            if pet["status"] == "dismissed":
                self._storage.setdefault(pet["owner_id"], []).append(item_id)
            else:
                pet["bag"].append(item_id)
            return copy.deepcopy(pet["bag"])

    def companion_bag(self, pet_id):
        with self._lock:
            return copy.deepcopy(self._pet(pet_id)["bag"])

    def storage_bag(self, player_id):
        with self._lock:
            return copy.deepcopy(self._storage.get(player_id, []))

    def quest_log(self, player_id):
        with self._lock:
            return copy.deepcopy(self._quest_logs.get(player_id, []))

    def learn_skill(self, pet_id, skill):
        with self._lock:
            pet = self._pet(pet_id)
            if skill not in pet["skills"]:
                pet["skills"].append(skill)
            return copy.deepcopy(pet["skills"])

    def move_pet(self, pet_id, position):
        with self._lock:
            pet = self._pet(pet_id)
            pet["position"] = tuple(position) if position is not None else None
            return pet["position"]

    def add_buff(self, pet_id, buff):
        with self._lock:
            pet = self._pet(pet_id)
            if buff not in pet["buffs"]:
                pet["buffs"].append(buff)
            return copy.deepcopy(pet["buffs"])

    def target_died(self, pet_id):
        with self._lock:
            pet = self._pet(pet_id)
            pet["target"] = None
            pet["command"] = "follow"
            pet["status"] = "following"
            return copy.deepcopy(pet)

    def stun(self, pet_id):
        with self._lock:
            pet = self._pet(pet_id)
            pet["stunned"] = True
            return {"status": "stunned", "pet_id": pet_id}

    def unstun(self, pet_id):
        with self._lock:
            pet = self._pet(pet_id)
            pet["stunned"] = False
            return {"status": "active", "pet_id": pet_id}

    def companion_state(self, pet_id):
        with self._lock:
            pet = self._pet(pet_id)
            return copy.deepcopy(
                {
                    "pet_id": pet["pet_id"],
                    "owner_id": pet["owner_id"],
                    "scene_id": pet["scene_id"],
                    "name": pet["name"],
                    "status": pet["status"],
                    "command": pet["command"],
                    "target": pet["target"],
                    "position": pet["position"],
                    "skills": pet["skills"],
                    "buffs": pet["buffs"],
                    "stunned": pet["stunned"],
                    "summoned": pet["summoned"],
                    "pending_commands": len(pet["queue"]),
                }
            )

    def snapshot(self):
        with self._lock:
            return {
                "version": 1,
                "server_time": self.server_time,
                "pets": copy.deepcopy(self._pets),
                "owner_sequences": copy.deepcopy(self._owner_sequences),
                "creature_owners": copy.deepcopy(self._creature_owners),
                "owner_scenes": copy.deepcopy(self._owner_scenes),
                "owner_alive": copy.deepcopy(self._owner_alive),
                "storage": copy.deepcopy(self._storage),
                "quest_logs": copy.deepcopy(self._quest_logs),
            }

    def restore(self, snapshot):
        with self._lock:
            self.server_time = snapshot.get("server_time", 0)
            self._pets = copy.deepcopy(snapshot.get("pets", {}))
            self._owner_sequences = copy.deepcopy(
                snapshot.get("owner_sequences", {})
            )
            self._creature_owners = copy.deepcopy(
                snapshot.get("creature_owners", {})
            )
            self._owner_scenes = copy.deepcopy(
                snapshot.get("owner_scenes", {})
            )
            self._owner_alive = copy.deepcopy(
                snapshot.get("owner_alive", {})
            )
            self._storage = copy.deepcopy(snapshot.get("storage", {}))
            self._quest_logs = copy.deepcopy(snapshot.get("quest_logs", {}))
            return self
