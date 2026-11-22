"""Pet breeding, cooldown, incubation and claim ledger.

Breeding is all-or-nothing for both parents and materials, eggs are claimed by
exactly one owner, inbreeding past the threshold is rejected and the server
clock owns every cooldown.
"""

import random

COOLDOWN = "cooldown"
PARENT_UNAVAILABLE = "parent_unavailable"
INBREEDING_BLOCKED = "inbreeding_blocked"
INCUBATION_FULL = "incubation_full"
EGG_CLAIMED = "egg_claimed"
EGG_NOT_READY = "egg_not_ready"
UNKNOWN_EGG = "unknown_egg"
UNKNOWN_PET = "unknown_pet"
INSUFFICIENT_MATERIALS = "insufficient_materials"

HIDDEN_TRAIT = "hidden"
TWINS_TRAIT = "twins"
HIDDEN_TRAIT_CHANCE = 0.1
INHERIT_CHANCE = 0.5


class BreedingError(Exception):
    def __init__(self, code, message=""):
        self.code = code
        super().__init__(code if not message else "%s: %s" % (code, message))


class InbreedingBlocked(BreedingError):
    pass


class IncubationQueueFull(BreedingError):
    pass


class PetBreedingGenetics:
    def __init__(self, incubation_capacity=4, incubation_seconds=300,
                 cooldown_seconds=600, inbreeding_threshold=0.125,
                 server_time=0, random_seed=7):
        self.incubation_capacity = incubation_capacity
        self.incubation_seconds = incubation_seconds
        self.cooldown_seconds = cooldown_seconds
        self.inbreeding_threshold = inbreeding_threshold
        self.server_time = server_time
        self.random_seed = random_seed
        self._pets = {}
        self._materials = {}
        self._eggs = {}
        self._incubation_index = {}
        self._breed_log = []
        self._egg_seq = 0
        self._batch_seq = 0
        self._pet_seq = 0

    def advance(self, seconds):
        self.server_time += seconds
        return self.server_time

    def now(self):
        return self.server_time

    def register_pet(self, pet_id, owner_id, parents=(), generation=1,
                     traits=()):
        self._pets[pet_id] = {
            "pet_id": pet_id,
            "owner_id": owner_id,
            "parents": tuple(parents),
            "generation": generation,
            "traits": tuple(traits),
            "on_market": False,
            "released": False,
            "cooldown_until": None,
            "egg_id": None,
        }
        return pet_id

    def grant_materials(self, owner_id, count):
        self._materials[owner_id] = self._materials.get(owner_id, 0) + count
        return self._materials[owner_id]

    def material_balance(self, owner_id):
        return self._materials.get(owner_id, 0)

    def _pet_or_raise(self, pet_id):
        pet = self._pets.get(pet_id)
        if pet is None:
            raise BreedingError(UNKNOWN_PET, pet_id)
        return pet

    def set_on_market(self, pet_id, on_market=True):
        self._pet_or_raise(pet_id)["on_market"] = bool(on_market)

    def release_pet(self, pet_id):
        pet = self._pet_or_raise(pet_id)
        pet["released"] = True
        pet["on_market"] = False

    def breeding_cooldown(self, pet_id):
        pet = self._pets.get(pet_id)
        if pet is None or pet["cooldown_until"] is None:
            return 0
        return self.cooldown_seconds

    def cooldown_remaining(self, pet_id):
        pet = self._pets.get(pet_id)
        if pet is None or pet["cooldown_until"] is None:
            return 0
        return max(0, pet["cooldown_until"] - self.server_time)

    def _ancestor_depths(self, pet_id):
        depths = {}
        stack = [(pet_id, 0)]
        while stack:
            current, depth = stack.pop()
            if current in depths and depths[current] <= depth:
                continue
            depths[current] = depth
            pet = self._pets.get(current)
            if pet is not None:
                for parent in pet["parents"]:
                    stack.append((parent, depth + 1))
        return depths

    def inbreeding_coefficient(self, father_id, mother_id):
        father_side = self._ancestor_depths(father_id)
        mother_side = self._ancestor_depths(mother_id)
        coefficient = 0.0
        for ancestor in set(father_side) & set(mother_side):
            coefficient += 0.5 ** (father_side[ancestor] +
                                   mother_side[ancestor] + 1)
        return coefficient

    def _roll_traits(self, parent_a, parent_b, egg_seq):
        rng = random.Random("%s:%s" % (self.random_seed, egg_seq))
        traits = []
        for trait in parent_a["traits"] + parent_b["traits"]:
            if trait in traits:
                continue
            if rng.random() < INHERIT_CHANCE:
                traits.append(trait)
        if rng.random() < HIDDEN_TRAIT_CHANCE:
            traits.append(HIDDEN_TRAIT)
        return tuple(traits)

    def breed(self, house_id, parent_a, parent_b, materials=2, client_luck=0):
        first = self._pets.get(parent_a)
        second = self._pets.get(parent_b)
        if (first is None or second is None
                or first["released"] or second["released"]
                or first["on_market"] or second["on_market"]):
            raise BreedingError(PARENT_UNAVAILABLE,
                                "%s/%s" % (parent_a, parent_b))
        if (self.cooldown_remaining(parent_a) > 0
                or self.cooldown_remaining(parent_b) > 0):
            raise BreedingError(COOLDOWN, "%s/%s" % (parent_a, parent_b))
        if (self.inbreeding_coefficient(parent_a, parent_b)
                >= self.inbreeding_threshold):
            raise InbreedingBlocked(INBREEDING_BLOCKED,
                                    "%s/%s" % (parent_a, parent_b))
        owner_id = first["owner_id"]
        twins = (TWINS_TRAIT in first["traits"]
                 or TWINS_TRAIT in second["traits"])
        egg_count = 2 if twins else 1
        active = sum(1 for egg in self._eggs.values()
                     if egg["status"] != "claimed")
        if active + egg_count > self.incubation_capacity:
            raise IncubationQueueFull(INCUBATION_FULL, house_id)
        if self._materials.get(owner_id, 0) < materials:
            raise BreedingError(INSUFFICIENT_MATERIALS, owner_id)

        self._materials[owner_id] -= materials
        cooldown_until = self.server_time + self.cooldown_seconds
        first["cooldown_until"] = cooldown_until
        second["cooldown_until"] = cooldown_until

        self._batch_seq += 1
        batch_id = "batch-%d" % self._batch_seq
        egg_ids = []
        for _ in range(egg_count):
            self._egg_seq += 1
            egg_id = "egg-%d" % self._egg_seq
            self._eggs[egg_id] = {
                "egg_id": egg_id,
                "batch_id": batch_id,
                "house_id": house_id,
                "owner_id": owner_id,
                "parents": (parent_a, parent_b),
                "traits": self._roll_traits(first, second, self._egg_seq),
                "laid_at": self.server_time,
                "hatch_at": self.server_time + self.incubation_seconds,
                "status": "incubating",
                "claimed_by": None,
                "claimed_at": None,
                "offspring_id": None,
            }
            self._incubation_index.setdefault(owner_id, []).append(egg_id)
            egg_ids.append(egg_id)
        self._breed_log.append({
            "batch_id": batch_id,
            "house_id": house_id,
            "owner_id": owner_id,
            "parents": (parent_a, parent_b),
            "egg_ids": tuple(egg_ids),
            "materials": materials,
            "server_time": self.server_time,
        })
        return {
            "batch_id": batch_id,
            "house_id": house_id,
            "owner_id": owner_id,
            "egg_ids": egg_ids,
            "materials_charged": materials,
        }

    def egg_state(self, egg_id):
        egg = self._eggs.get(egg_id)
        if egg is None:
            raise BreedingError(UNKNOWN_EGG, egg_id)
        if egg["status"] == "claimed":
            remaining = 0
            status = "claimed"
        else:
            remaining = max(0, egg["hatch_at"] - self.server_time)
            status = "ready" if remaining == 0 else "incubating"
        return {
            "egg_id": egg_id,
            "batch_id": egg["batch_id"],
            "house_id": egg["house_id"],
            "owner_id": egg["owner_id"],
            "parents": egg["parents"],
            "traits": egg["traits"],
            "status": status,
            "remaining": remaining,
            "claimed_by": egg["claimed_by"],
            "offspring_id": egg["offspring_id"],
        }

    def incubating(self, owner_id=None):
        if owner_id is None:
            return [egg_id for ids in self._incubation_index.values()
                    for egg_id in ids]
        return list(self._incubation_index.get(owner_id, []))

    def boost(self, egg_id, seconds):
        egg = self._eggs.get(egg_id)
        if egg is None:
            raise BreedingError(UNKNOWN_EGG, egg_id)
        if egg["status"] == "claimed":
            raise BreedingError(EGG_CLAIMED, egg_id)
        egg["hatch_at"] = max(self.server_time, egg["hatch_at"] - seconds)
        return self.egg_state(egg_id)

    def claim_egg(self, egg_id, owner_id):
        egg = self._eggs.get(egg_id)
        if egg is None:
            raise BreedingError(UNKNOWN_EGG, egg_id)
        if egg["status"] == "claimed":
            return {"status": EGG_CLAIMED, "egg_id": egg_id}
        remaining = max(0, egg["hatch_at"] - self.server_time)
        if remaining > 0:
            return {"status": EGG_NOT_READY, "egg_id": egg_id,
                    "remaining": remaining}

        egg["status"] = "claimed"
        egg["claimed_by"] = owner_id
        egg["claimed_at"] = self.server_time
        if egg_id in self._incubation_index.get(egg["owner_id"], []):
            self._incubation_index[egg["owner_id"]].remove(egg_id)

        self._pet_seq += 1
        pet_id = "offspring-%d" % self._pet_seq
        generation = 1
        for parent_id in egg["parents"]:
            parent = self._pets.get(parent_id)
            if parent is not None:
                generation = max(generation, parent["generation"] + 1)
        self._pets[pet_id] = {
            "pet_id": pet_id,
            "owner_id": owner_id,
            "parents": egg["parents"],
            "generation": generation,
            "traits": egg["traits"],
            "on_market": False,
            "released": False,
            "cooldown_until": None,
            "egg_id": egg_id,
        }
        egg["offspring_id"] = pet_id
        return {
            "status": "claimed",
            "egg_id": egg_id,
            "batch_id": egg["batch_id"],
            "pet_id": pet_id,
            "owner_id": owner_id,
            "traits": egg["traits"],
        }

    def batch_claim(self, egg_ids, owner_id):
        claimed = []
        skipped = {}
        for egg_id in egg_ids:
            try:
                result = self.claim_egg(egg_id, owner_id)
            except BreedingError as exc:
                skipped[egg_id] = exc.code
                continue
            if result["status"] == "claimed":
                claimed.append(egg_id)
            else:
                skipped[egg_id] = result["status"]
        return {"claimed": claimed, "skipped": skipped}

    def owned_pets(self, owner_id):
        return [pet_id for pet_id, pet in self._pets.items()
                if pet["owner_id"] == owner_id and not pet["released"]]

    def snapshot(self):
        return {
            "incubation_capacity": self.incubation_capacity,
            "incubation_seconds": self.incubation_seconds,
            "cooldown_seconds": self.cooldown_seconds,
            "inbreeding_threshold": self.inbreeding_threshold,
            "server_time": self.server_time,
            "random_seed": self.random_seed,
            "egg_seq": self._egg_seq,
            "batch_seq": self._batch_seq,
            "pet_seq": self._pet_seq,
            "materials": dict(self._materials),
            "pets": {pet_id: dict(pet) for pet_id, pet in self._pets.items()},
            "eggs": {egg_id: dict(egg) for egg_id, egg in self._eggs.items()},
            "breed_log": [dict(record) for record in self._breed_log],
        }

    def restore(self, snapshot):
        self.incubation_capacity = snapshot["incubation_capacity"]
        self.incubation_seconds = snapshot["incubation_seconds"]
        self.cooldown_seconds = snapshot["cooldown_seconds"]
        self.inbreeding_threshold = snapshot["inbreeding_threshold"]
        self.server_time = snapshot["server_time"]
        self.random_seed = snapshot["random_seed"]
        self._egg_seq = snapshot["egg_seq"]
        self._batch_seq = snapshot["batch_seq"]
        self._pet_seq = snapshot["pet_seq"]
        self._materials = dict(snapshot["materials"])
        self._pets = {pet_id: dict(pet)
                      for pet_id, pet in snapshot["pets"].items()}
        self._eggs = {egg_id: dict(egg)
                      for egg_id, egg in snapshot["eggs"].items()}
        self._breed_log = [dict(record) for record in snapshot["breed_log"]]
        self.recover()

    def recover(self):
        self._incubation_index = {}
        for egg_id, egg in self._eggs.items():
            if egg["status"] == "claimed":
                continue
            self._incubation_index.setdefault(egg["owner_id"], []).append(
                egg_id)
        return self.incubating()
