"""Companion command queue, follow/attack and scene migration (skeleton).

A wild creature is tamed once, command sequence numbers are global per owner,
migration is atomic with the owner and snapshots preserve pet state without
duplicating bag items.  No behaviour is implemented yet.
"""

ALREADY_TAMED = "already_tamed"
OLD_SEQUENCE = "old_sequence"
MIGRATION_FAILED = "migration_failed"
ALREADY_ALIVE = "already_alive"
OWNER_DEAD = "owner_dead"
ALREADY_SUMMONED = "already_summoned"
UNKNOWN_PET = "unknown_pet"
STUNNED = "stunned"


class CompanionError(Exception):
    def __init__(self, code, message=""):
        self.code = code
        super().__init__(code if not message else "%s: %s" % (code, message))


class CompanionAiCommand:
    def __init__(self, server_time=0, migration_hook=None):
        self.server_time = server_time
        self.migration_hook = migration_hook

    def advance(self, seconds):
        raise NotImplementedError

    def register_pet(self, pet_id, owner_id, scene_id="scene-1", name=None):
        raise NotImplementedError

    def tame(self, creature_id, player_id):
        raise NotImplementedError

    def creature_owner(self, creature_id):
        raise NotImplementedError

    def issue_command(self, pet_id, seq, command, target=None):
        raise NotImplementedError

    def pending_commands(self, pet_id):
        raise NotImplementedError

    def execute_next(self, pet_id):
        raise NotImplementedError

    def follow(self, pet_id):
        raise NotImplementedError

    def recall(self, pet_id):
        raise NotImplementedError

    def migrate(self, pet_id, scene_id):
        raise NotImplementedError

    def pet_died(self, pet_id):
        raise NotImplementedError

    def revive(self, pet_id):
        raise NotImplementedError

    def owner_died(self, player_id):
        raise NotImplementedError

    def revive_owner(self, player_id):
        raise NotImplementedError

    def summon(self, pet_id):
        raise NotImplementedError

    def dismiss(self, pet_id):
        raise NotImplementedError

    def rename_pet(self, pet_id, name):
        raise NotImplementedError

    def auto_pickup(self, pet_id, item_id, quest_item=False):
        raise NotImplementedError

    def add_bag_item(self, pet_id, item_id):
        raise NotImplementedError

    def companion_bag(self, pet_id):
        raise NotImplementedError

    def storage_bag(self, player_id):
        raise NotImplementedError

    def quest_log(self, player_id):
        raise NotImplementedError

    def learn_skill(self, pet_id, skill):
        raise NotImplementedError

    def move_pet(self, pet_id, position):
        raise NotImplementedError

    def add_buff(self, pet_id, buff):
        raise NotImplementedError

    def target_died(self, pet_id):
        raise NotImplementedError

    def stun(self, pet_id):
        raise NotImplementedError

    def unstun(self, pet_id):
        raise NotImplementedError

    def companion_state(self, pet_id):
        raise NotImplementedError

    def snapshot(self):
        raise NotImplementedError

    def restore(self, snapshot):
        raise NotImplementedError
