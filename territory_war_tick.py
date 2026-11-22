"""Territory war tick engine.

Baseline shell: every method raises NotImplementedError.

Contract notes for the implementation:

* ``tick(now)`` advances in fixed ``tick_seconds`` steps.  The first call
  only fixes the baseline.  A long gap is capped at ``max_catchup_ticks``
  and the remainder is reported in ``skipped_ticks`` and discarded.
  Concurrent tick calls for the same instant must apply a single tick.
* A point with two or more contesting factions resets its progress to zero
  every tick and cannot be captured.  A sole faction accumulates one point
  per tick; reaching ``capture_progress`` assigns the owner, awards
  ``capture_award`` to that faction and resets progress.  Once owned, the
  owner earns one score per tick while uncontested, and the owner reported
  by ``state`` is the same faction that receives the score.
* Lanes are isolated: capturing in one lane changes only that lane's
  ownership; reinforcements and contesting factions never move across
  lanes.
* During a truce no progress, capture, score or kill score is applied.
* ``respawn`` consumes one reinforcement per lane/faction; at zero it is
  refused and the reported count stays zero.
* ``record_kill`` counts once per account per round; a second character on
  the same account returns ``account_already_counted``.  Leaving the match
  freezes that character's contribution.
* ``report_capture`` is idempotent by report id.  ``settle_round`` is
  idempotent by round id and does not re-send mails.  ``claim_reward`` pays
  once per round/account.
* ``switch_faction`` during an active round raises ``FactionLockedError``.
* Positions outside the point boundary raise ``OutOfBoundsError``.
* ``export_state`` / ``load_state`` preserve owners, progress, contesting
  factions, reinforcements and the tick baseline across a restart.
"""


class FactionLockedError(Exception):
    """Raised when a character tries to switch faction mid-round."""


class OutOfBoundsError(Exception):
    """Raised when a player enters a point from outside its boundary."""


class TerritoryWarTick:
    def __init__(
        self,
        tick_seconds=5,
        capture_progress=3,
        max_catchup_ticks=2,
        capture_award=10,
    ):
        raise NotImplementedError

    def add_point(self, point_id, lane, boundary=(0, 0, 10, 10)):
        raise NotImplementedError

    def join_match(self, character, faction, account=None):
        raise NotImplementedError

    def leave_match(self, character):
        raise NotImplementedError

    def enter(self, point_id, faction, character=None, position=None):
        raise NotImplementedError

    def leave(self, point_id, faction):
        raise NotImplementedError

    def tick(self, now=0.0):
        raise NotImplementedError

    def state(self, point_id):
        raise NotImplementedError

    def lane_state(self, lane):
        raise NotImplementedError

    def score(self, faction):
        raise NotImplementedError

    def record_kill(self, character, points=1, now=0.0):
        raise NotImplementedError

    def contribution(self, account):
        raise NotImplementedError

    def set_truce(self, start, end):
        raise NotImplementedError

    def set_reinforcements(self, lane, faction, count):
        raise NotImplementedError

    def reinforcements(self, lane, faction):
        raise NotImplementedError

    def respawn(self, lane, faction, character):
        raise NotImplementedError

    def report_capture(self, point_id, faction, report_id):
        raise NotImplementedError

    def settle_round(self, round_id):
        raise NotImplementedError

    def claim_reward(self, round_id, account):
        raise NotImplementedError

    def switch_faction(self, character, faction):
        raise NotImplementedError

    def export_state(self):
        raise NotImplementedError

    def load_state(self, state):
        raise NotImplementedError
