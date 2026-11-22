"""Roguelike run seed, random stream, floor snapshot and settlement (skeleton).

One run owns one deterministic random stream, chest rewards are idempotent,
recovery returns to the last committed floor and daily settlement happens
once per account and server day.  No behaviour is implemented yet.
"""

UNKNOWN_RUN = "unknown_run"
ALREADY_LOOTED = "already_looted"
ALREADY_JOINED = "already_joined"
ALREADY_IN_RUN = "already_in_run"
ALREADY_SETTLED = "already_settled"
ITEM_CONSUMED = "item_consumed"
CONTENT_MISMATCH = "content_mismatch"


class RunError(Exception):
    def __init__(self, code, message=""):
        self.code = code
        super().__init__(code if not message else "%s: %s" % (code, message))


class RoguelikeRunSeed:
    def __init__(self, server_day=1, seed_base=90210, content_version="v1"):
        self.server_day = server_day
        self.seed_base = seed_base
        self.content_version = content_version

    def advance(self, seconds):
        raise NotImplementedError

    def set_server_day(self, day):
        raise NotImplementedError

    def start_run(self, run_id, account_id, seed=None, mode="normal"):
        raise NotImplementedError

    def run_state(self, run_id):
        raise NotImplementedError

    def daily_seed(self, account_id, client_day=None):
        raise NotImplementedError

    def next_random(self, run_id, stream="shared"):
        raise NotImplementedError

    def open_chest(self, run_id, floor, chest_id):
        raise NotImplementedError

    def grant_item(self, run_id, item_id, player_id=None, count=1):
        raise NotImplementedError

    def pick_up(self, run_id, item_id, player_id=None):
        raise NotImplementedError

    def commit_floor(self, run_id, floor, state):
        raise NotImplementedError

    def recover(self, run_id):
        raise NotImplementedError

    def join_run(self, run_id, player_id):
        raise NotImplementedError

    def party_relics(self, run_id, player_id):
        raise NotImplementedError

    def settle_daily(self, account_id, run_id):
        raise NotImplementedError

    def use_revive(self, run_id, item_id, player_id):
        raise NotImplementedError

    def revive_count(self, run_id):
        raise NotImplementedError

    def inventory(self, run_id, player_id=None):
        raise NotImplementedError

    def leaderboard(self):
        raise NotImplementedError

    def snapshot(self):
        raise NotImplementedError

    def restore(self, snapshot):
        raise NotImplementedError
