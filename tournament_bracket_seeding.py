"""Tournament bracket seeding, byes, advancement and audit (skeleton).

Results advance a match once, a slot accepts one winner, forfeits end the
match and cancel the reward together, and snapshots preserve the bracket.
No behaviour is implemented yet.
"""

BRACKET_ADVANCED = "bracket_advanced"
MATCH_FINISHED = "match_finished"
MATCH_NOT_FOUND = "match_not_found"
BYE_MATCH = "bye_match"
STALE_BRACKET = "stale_bracket"
NOT_STARTED = "not_started"
SLOT_CONFLICT = "slot_conflict"


class BracketError(Exception):
    def __init__(self, code, message=""):
        self.code = code
        super().__init__(code if not message else "%s: %s" % (code, message))


class TournamentBracketSeeding:
    def __init__(self, size=8, server_time=0):
        self.size = size
        self.server_time = server_time

    def advance(self, seconds):
        raise NotImplementedError

    def seed(self, entrants):
        raise NotImplementedError

    def seeding_order(self):
        raise NotImplementedError

    def start(self):
        raise NotImplementedError

    def matches(self, round_no):
        raise NotImplementedError

    def third_place_match(self):
        raise NotImplementedError

    def match(self, match_id):
        raise NotImplementedError

    def report_result(self, match_id, score, operator="ref"):
        raise NotImplementedError

    def commit_slot(self, round_no, slot, match_id, winner, operator="ref"):
        raise NotImplementedError

    def forfeit(self, match_id, player_id, operator="ref"):
        raise NotImplementedError

    def reschedule(self, match_id, new_start, operator="ref"):
        raise NotImplementedError

    def replay(self, match_id):
        raise NotImplementedError

    def conflicts(self):
        raise NotImplementedError

    def resolve_conflict(self, conflict_id, winner, operator="ref"):
        raise NotImplementedError

    def slot_winner(self, round_no, slot):
        raise NotImplementedError

    def match_history(self, match_id):
        raise NotImplementedError

    def pending_rewards(self):
        raise NotImplementedError

    def current_version(self):
        raise NotImplementedError

    def view(self, version):
        raise NotImplementedError

    def snapshot(self):
        raise NotImplementedError

    def restore(self, snapshot):
        raise NotImplementedError

    def recover(self):
        raise NotImplementedError
