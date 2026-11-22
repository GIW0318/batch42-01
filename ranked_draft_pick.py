"""Ranked draft pick/ban phase, swaps and reconnect (skeleton).

A champion can be locked once, phases follow the server clock, dodging frees
all seats atomically and reconnects restore the live phase.  No behaviour is
implemented yet.
"""

CHAMPION_TAKEN = "champion_taken"
UNKNOWN_CHAMPION = "unknown_champion"
WRONG_PHASE = "wrong_phase"
ALREADY_PICKED = "already_picked"
DRAFT_INCOMPLETE = "draft_incomplete"
GAME_STARTED = "game_started"
ALREADY_RELEASED = "already_released"
PERSIST_FAILED = "persist_failed"
SWAP_CONFLICT = "swap_conflict"


class DraftError(Exception):
    def __init__(self, code, message=""):
        self.code = code
        super().__init__(code if not message else "%s: %s" % (code, message))


class RankedDraftPick:
    def __init__(self, server_time=0, ban_seconds=30, pick_seconds=30,
                 champion_pool=()):
        self.server_time = server_time
        self.ban_seconds = ban_seconds
        self.pick_seconds = pick_seconds
        self.champion_pool = list(champion_pool)

    def advance(self, seconds):
        raise NotImplementedError

    def now(self):
        raise NotImplementedError

    def start_draft(self, match_id, blue_team, red_team, champion_pool=None):
        raise NotImplementedError

    def phase_state(self, match_id):
        raise NotImplementedError

    def ban(self, match_id, player_id, champion):
        raise NotImplementedError

    def pick(self, match_id, player_id, champion):
        raise NotImplementedError

    def advance_phase(self, match_id):
        raise NotImplementedError

    def lineup(self, match_id):
        raise NotImplementedError

    def open_seats(self, match_id):
        raise NotImplementedError

    def reconnect(self, match_id, player_id):
        raise NotImplementedError

    def request_swap(self, match_id, from_player, to_player):
        raise NotImplementedError

    def accept_swap(self, match_id, swap_id):
        raise NotImplementedError

    def dodge(self, match_id, player_id):
        raise NotImplementedError

    def start_game(self, match_id, persist_hook=None):
        raise NotImplementedError

    def spectators_view(self, match_id):
        raise NotImplementedError

    def snapshot(self):
        raise NotImplementedError

    def restore(self, snapshot):
        raise NotImplementedError
