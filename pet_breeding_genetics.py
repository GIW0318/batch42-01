"""Pet breeding, cooldown, incubation and claim ledger (skeleton).

Breeding is all-or-nothing for both parents and materials, eggs are claimed by
exactly one owner, inbreeding past the threshold is rejected and the server
clock owns every cooldown.  No behaviour is implemented yet.
"""

COOLDOWN = "cooldown"
PARENT_UNAVAILABLE = "parent_unavailable"
INBREEDING_BLOCKED = "inbreeding_blocked"
INCUBATION_FULL = "incubation_full"
EGG_CLAIMED = "egg_claimed"
EGG_NOT_READY = "egg_not_ready"
UNKNOWN_EGG = "unknown_egg"
INSUFFICIENT_MATERIALS = "insufficient_materials"


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

    def advance(self, seconds):
        raise NotImplementedError

    def now(self):
        raise NotImplementedError

    def register_pet(self, pet_id, owner_id, parents=(), generation=1,
                     traits=()):
        raise NotImplementedError

    def grant_materials(self, owner_id, count):
        raise NotImplementedError

    def material_balance(self, owner_id):
        raise NotImplementedError

    def set_on_market(self, pet_id, on_market=True):
        raise NotImplementedError

    def release_pet(self, pet_id):
        raise NotImplementedError

    def breeding_cooldown(self, pet_id):
        raise NotImplementedError

    def cooldown_remaining(self, pet_id):
        raise NotImplementedError

    def inbreeding_coefficient(self, father_id, mother_id):
        raise NotImplementedError

    def breed(self, house_id, parent_a, parent_b, materials=2, client_luck=0):
        raise NotImplementedError

    def egg_state(self, egg_id):
        raise NotImplementedError

    def incubating(self, owner_id=None):
        raise NotImplementedError

    def boost(self, egg_id, seconds):
        raise NotImplementedError

    def claim_egg(self, egg_id, owner_id):
        raise NotImplementedError

    def batch_claim(self, egg_ids, owner_id):
        raise NotImplementedError

    def owned_pets(self, owner_id):
        raise NotImplementedError

    def snapshot(self):
        raise NotImplementedError

    def restore(self, snapshot):
        raise NotImplementedError

    def recover(self):
        raise NotImplementedError
