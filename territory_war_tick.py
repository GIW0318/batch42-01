"""Territory war tick engine.

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

import copy
import math
import threading


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
        if tick_seconds <= 0:
            raise ValueError("tick_seconds must be positive")
        if capture_progress <= 0:
            raise ValueError("capture_progress must be positive")
        if max_catchup_ticks < 0:
            raise ValueError("max_catchup_ticks cannot be negative")

        self.tick_seconds = tick_seconds
        self.capture_progress = capture_progress
        self.max_catchup_ticks = int(max_catchup_ticks)
        self.capture_award = capture_award

        self._lock = threading.RLock()

        # point_id -> {lane, boundary, owner, progress, contest}
        self._points = {}
        self._lane_points = {}

        # lane -> faction -> remaining reinforcements
        self._reinforcements = {}

        # Capture scoreboard.  This is the only faction score source so that
        # the visible owner and the faction being credited cannot diverge.
        self._scores = {}

        # character -> {faction, account, in_match}
        self._characters = {}
        self._contribution_total = {}
        self._round_contribution = {}
        self._round_counted_accounts = set()
        self._round_active = False

        self._last_tick = None
        self._truce = None

        self._capture_reports = {}
        # round_id -> immutable settlement payload (including contributions).
        self._settlements = {}
        self._reward_claims = {}

    def add_point(self, point_id, lane, boundary=(0, 0, 10, 10)):
        normalized = self._normalize_boundary(boundary)
        with self._lock:
            point = self._points.get(point_id)
            if point is None:
                point = {
                    "lane": lane,
                    "boundary": normalized,
                    "owner": None,
                    "progress": 0,
                    "contest": set(),
                }
                self._points[point_id] = point
                self._lane_points.setdefault(lane, set()).add(point_id)
            else:
                old_lane = point["lane"]
                if old_lane != lane:
                    self._lane_points.get(old_lane, set()).discard(point_id)
                    self._lane_points.setdefault(lane, set()).add(point_id)
                point["lane"] = lane
                point["boundary"] = normalized
            return self._state_locked(point_id)

    def join_match(self, character, faction, account=None):
        with self._lock:
            existing = self._characters.get(character)
            if account is None:
                # A reconnect that omits the account keeps the original
                # identity, so it cannot be used to count the same player
                # again under a new key.
                account = (
                    existing["account"] if existing is not None else character
                )
            if existing is None:
                self._characters[character] = {
                    "faction": faction,
                    "account": account,
                    "in_match": True,
                }
            else:
                if self._round_active and existing["faction"] != faction:
                    raise FactionLockedError(
                        "cannot change faction during an active round"
                    )
                existing["faction"] = faction
                existing["account"] = account
                existing["in_match"] = True
            self._round_active = True
            return {
                "character": character,
                "faction": faction,
                "account": account,
                "joined": True,
            }

    def leave_match(self, character):
        with self._lock:
            character_state = self._characters.get(character)
            if character_state is None:
                return {
                    "character": character,
                    "left": False,
                    "frozen_contribution": 0,
                }
            character_state["in_match"] = False
            account = character_state["account"]
            return {
                "character": character,
                "left": True,
                "frozen_contribution": self._contribution_total.get(account, 0),
            }

    def enter(self, point_id, faction, character=None, position=None):
        with self._lock:
            point = self._points.get(point_id)
            if point is None:
                raise KeyError(point_id)
            if position is not None:
                self._check_position(point["boundary"], position)

            contest = point["contest"]
            if faction not in contest:
                if contest:
                    # Any change from a sole occupant into a contest (or a
                    # change of the sole occupant) invalidates the progress
                    # that was accumulated by the previous lineup.
                    point["progress"] = 0
                contest.add(faction)
            elif len(contest) >= 2:
                point["progress"] = 0
            self._round_active = True
            return self._state_locked(point_id)

    def leave(self, point_id, faction):
        with self._lock:
            point = self._points.get(point_id)
            if point is None:
                raise KeyError(point_id)
            contest = point["contest"]
            if faction in contest:
                contest.remove(faction)
                # Progress belongs to a particular contest, not to the
                # surviving faction.  Never let an old bar be inherited.
                point["progress"] = 0
            return self._state_locked(point_id)

    def tick(self, now=0.0):
        with self._lock:
            if self._last_tick is None:
                self._last_tick = now
                return {"ticks": 0, "skipped_ticks": 0, "captured": []}

            elapsed = now - self._last_tick
            if elapsed <= 0:
                return {"ticks": 0, "skipped_ticks": 0, "captured": []}

            # A small tolerance keeps integer/exact-step callers from losing
            # a tick to binary floating point noise.
            tolerance = 1e-7 * max(1.0, abs(elapsed))
            due = int(math.floor((elapsed + tolerance) / self.tick_seconds))
            if due <= 0:
                return {"ticks": 0, "skipped_ticks": 0, "captured": []}

            applied = min(due, self.max_catchup_ticks)
            skipped = due - applied
            captured = []

            # Replay in chronological order.  If the gap exceeds the cap the
            # unprocessed tail is discarded and the baseline jumps to now.
            for index in range(applied):
                tick_time = self._last_tick + (index + 1) * self.tick_seconds
                captured.extend(self._advance_tick_locked(tick_time))

            if skipped:
                self._last_tick = now
            else:
                self._last_tick += applied * self.tick_seconds

            return {
                "ticks": applied,
                "skipped_ticks": skipped,
                "captured": captured,
            }

    def state(self, point_id):
        with self._lock:
            return self._state_locked(point_id)

    def lane_state(self, lane):
        with self._lock:
            points = {}
            for point_id in self._lane_points.get(lane, set()):
                points[point_id] = self._state_locked(point_id)
            return {
                "lane": lane,
                "points": points,
                "reinforcements": copy.deepcopy(
                    self._reinforcement_pool(lane)
                ),
            }

    def score(self, faction):
        with self._lock:
            return self._scores.get(faction, 0)

    def record_kill(self, character, points=1, now=0.0):
        with self._lock:
            if self._in_truce(now):
                return {"accepted": False, "reason": "truce"}

            character_state = self._characters.get(character)
            if character_state is None or not character_state["in_match"]:
                return {"accepted": False, "reason": "not_in_match"}

            account = character_state["account"]
            if account in self._round_counted_accounts:
                return {
                    "accepted": False,
                    "reason": "account_already_counted",
                }

            self._round_counted_accounts.add(account)
            self._round_contribution[account] = (
                self._round_contribution.get(account, 0) + points
            )
            self._contribution_total[account] = (
                self._contribution_total.get(account, 0) + points
            )
            self._round_active = True
            return {
                "accepted": True,
                "account": account,
                "points": points,
                "contribution": self._contribution_total[account],
            }

    def contribution(self, account):
        with self._lock:
            return self._contribution_total.get(account, 0)

    def set_truce(self, start, end):
        with self._lock:
            if end < start:
                raise ValueError("truce end cannot precede its start")
            self._truce = (start, end)
            return {"start": start, "end": end}

    def set_reinforcements(self, lane, faction, count):
        with self._lock:
            count = max(0, int(count))
            self._reinforcement_pool(lane, create=True)[faction] = count
            return count

    def reinforcements(self, lane, faction):
        with self._lock:
            return self._reinforcement_pool(lane).get(faction, 0)

    def respawn(self, lane, faction, character):
        with self._lock:
            character_state = self._characters.get(character)
            if character_state is not None and not character_state["in_match"]:
                return {
                    "allowed": False,
                    "remaining": self._reinforcement_pool(lane).get(
                        faction, 0
                    ),
                    "reason": "not_in_match",
                }

            lane_pool = self._reinforcement_pool(lane, create=True)
            remaining = lane_pool.get(faction, 0)
            if remaining <= 0:
                lane_pool[faction] = 0
                return {
                    "allowed": False,
                    "remaining": 0,
                    "reason": "no reinforcements",
                }

            remaining -= 1
            lane_pool[faction] = remaining
            self._round_active = True
            return {"allowed": True, "remaining": remaining}

    def report_capture(self, point_id, faction, report_id):
        with self._lock:
            previous = self._capture_reports.get(report_id)
            if previous is not None:
                result = copy.deepcopy(previous)
                result["status"] = "duplicate"
                return result

            point = self._points.get(point_id)
            if point is None:
                raise KeyError(point_id)

            event_time = 0.0 if self._last_tick is None else self._last_tick
            if self._in_truce(event_time):
                return {
                    "status": "truce",
                    "point": point_id,
                    "faction": faction,
                }

            contest = point["contest"]
            if len(contest) >= 2:
                return {
                    "status": "contested",
                    "point": point_id,
                    "faction": faction,
                }
            if contest and faction not in contest:
                return {
                    "status": "not_contesting",
                    "point": point_id,
                    "faction": faction,
                }

            if point["owner"] == faction:
                result = {
                    "status": "already_owned",
                    "point": point_id,
                    "faction": faction,
                }
                self._capture_reports[report_id] = copy.deepcopy(result)
                return result
            if point["owner"] is not None and not contest:
                # A stale/duplicate event from another faction cannot flip an
                # owned point when that faction has no presence there.
                return {
                    "status": "not_contesting",
                    "point": point_id,
                    "faction": faction,
                }

            # Capture is the single transition that changes ownership and
            # awards the capture bonus.  Both use the same faction value.
            point["owner"] = faction
            point["progress"] = 0
            self._scores[faction] = (
                self._scores.get(faction, 0) + self.capture_award
            )
            self._round_active = True
            result = {
                "status": "captured",
                "point": point_id,
                "faction": faction,
            }
            self._capture_reports[report_id] = copy.deepcopy(result)
            return result

    def settle_round(self, round_id):
        with self._lock:
            settled = self._settlement_for(round_id)
            if settled is not None:
                result = copy.deepcopy(settled)
                result["duplicate"] = True
                return result

            contributions = {
                account: amount
                for account, amount in self._round_contribution.items()
                if amount > 0
            }
            scores = dict(self._scores)
            result = {
                "duplicate": False,
                "round": round_id,
                "mails": len(contributions),
                "scores": scores,
            }
            self._settlements[round_id] = {
                "duplicate": False,
                "round": round_id,
                "mails": len(contributions),
                "scores": copy.deepcopy(scores),
                "contributions": copy.deepcopy(contributions),
            }

            # The next kill/contribution belongs to the next round.  Scores
            # and already-sent mails for this round remain frozen above.
            self._round_contribution = {}
            self._round_counted_accounts = set()
            self._round_active = False
            return result

    def claim_reward(self, round_id, account):
        with self._lock:
            settlement = self._settlement_for(round_id)
            canonical_round = (
                settlement["round"] if settlement is not None else round_id
            )
            claim_key = (canonical_round, account)
            if claim_key in self._reward_claims:
                result = copy.deepcopy(self._reward_claims[claim_key])
                result["status"] = "already_claimed"
                return result

            if settlement is None:
                return {
                    "status": "not_settled",
                    "round": round_id,
                    "account": account,
                    "reward": 0,
                }

            reward = settlement["contributions"].get(account, 0)
            result = {
                "status": "claimed",
                "round": round_id,
                "account": account,
                "reward": reward,
            }
            self._reward_claims[claim_key] = copy.deepcopy(result)
            return result

    def switch_faction(self, character, faction):
        with self._lock:
            character_state = self._characters.get(character)
            current = None if character_state is None else character_state[
                "faction"
            ]
            if self._round_active and current != faction:
                raise FactionLockedError(
                    "cannot switch faction during an active round"
                )

            if character_state is None:
                character_state = {
                    "faction": faction,
                    "account": character,
                    "in_match": False,
                }
                self._characters[character] = character_state
            else:
                character_state["faction"] = faction
            return {
                "character": character,
                "faction": faction,
                "switched": current != faction,
            }

    def export_state(self):
        with self._lock:
            points = {}
            for point_id, point in self._points.items():
                points[point_id] = {
                    "lane": point["lane"],
                    "boundary": list(point["boundary"]),
                    "owner": point["owner"],
                    "progress": point["progress"],
                    "contest": sorted(point["contest"], key=repr),
                }

            return copy.deepcopy(
                {
                    "config": {
                        "tick_seconds": self.tick_seconds,
                        "capture_progress": self.capture_progress,
                        "max_catchup_ticks": self.max_catchup_ticks,
                        "capture_award": self.capture_award,
                    },
                    "points": points,
                    "scores": dict(self._scores),
                    "reinforcements": self._reinforcements,
                    "characters": self._characters,
                    "contribution_total": self._contribution_total,
                    "round_contribution": self._round_contribution,
                    "round_counted_accounts": list(
                        sorted(self._round_counted_accounts, key=repr)
                    ),
                    "round_active": self._round_active,
                    "last_tick": self._last_tick,
                    "truce": self._truce,
                    "capture_reports": self._capture_reports,
                    "settlements": self._settlements,
                    "reward_claims": [
                        {
                            "round": round_id,
                            "account": account,
                            "reward": payload["reward"],
                        }
                        for (round_id, account), payload in (
                            self._reward_claims.items()
                        )
                    ],
                }
            )

    def load_state(self, state):
        with self._lock:
            config = state.get("config")
            if config:
                self.tick_seconds = config.get(
                    "tick_seconds", self.tick_seconds
                )
                self.capture_progress = config.get(
                    "capture_progress", self.capture_progress
                )
                self.max_catchup_ticks = config.get(
                    "max_catchup_ticks", self.max_catchup_ticks
                )
                self.capture_award = config.get(
                    "capture_award", self.capture_award
                )

            self._points = {}
            self._lane_points = {}
            for point_id, point in state.get("points", {}).items():
                restored = {
                    "lane": point["lane"],
                    "boundary": self._normalize_boundary(point["boundary"]),
                    "owner": point.get("owner"),
                    "progress": point.get("progress", 0),
                    "contest": set(point.get("contest", [])),
                }
                self._points[point_id] = restored
                self._lane_points.setdefault(restored["lane"], set()).add(
                    point_id
                )

            self._scores = dict(state.get("scores", {}))

            self._reinforcements = {}
            for lane, pool in state.get("reinforcements", {}).items():
                self._reinforcements[lane] = {
                    faction: max(0, int(count))
                    for faction, count in pool.items()
                }

            self._characters = copy.deepcopy(state.get("characters", {}))
            self._contribution_total = dict(
                state.get("contribution_total", {})
            )
            self._round_contribution = dict(
                state.get("round_contribution", {})
            )
            self._round_counted_accounts = set(
                state.get("round_counted_accounts", [])
            )
            self._round_active = bool(state.get("round_active", False))
            self._last_tick = state.get("last_tick")

            truce = state.get("truce")
            self._truce = None if truce is None else tuple(truce)

            self._capture_reports = copy.deepcopy(
                state.get("capture_reports", {})
            )
            self._settlements = copy.deepcopy(state.get("settlements", {}))
            for settlement in self._settlements.values():
                settlement["contributions"] = dict(
                    settlement.get("contributions", {})
                )

            self._reward_claims = {}
            for claim in state.get("reward_claims", []):
                round_id = claim["round"]
                account = claim["account"]
                self._reward_claims[(round_id, account)] = {
                    "status": "claimed",
                    "round": round_id,
                    "account": account,
                    "reward": claim.get("reward", 0),
                }

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _normalize_boundary(boundary):
        if len(boundary) == 2 and hasattr(boundary[0], "__len__"):
            first, second = boundary
            boundary = (first[0], first[1], second[0], second[1])
        if len(boundary) != 4:
            raise ValueError("boundary must contain four coordinates")
        x1, y1, x2, y2 = (float(value) for value in boundary)
        left, right = sorted((x1, x2))
        bottom, top = sorted((y1, y2))
        return (left, bottom, right, top)

    @staticmethod
    def _position_xy(position):
        if isinstance(position, dict):
            return float(position["x"]), float(position["y"])
        if hasattr(position, "x") and hasattr(position, "y"):
            return float(position.x), float(position.y)
        return float(position[0]), float(position[1])

    def _check_position(self, boundary, position):
        try:
            x, y = self._position_xy(position)
        except (KeyError, IndexError, TypeError, ValueError):
            raise OutOfBoundsError("invalid position") from None
        left, bottom, right, top = boundary
        if not (left <= x <= right and bottom <= y <= top):
            raise OutOfBoundsError(
                "position ({}, {}) is outside the point boundary".format(x, y)
            )

    def _in_truce(self, now):
        if self._truce is None:
            return False
        start, end = self._truce
        return start <= now < end

    def _reinforcement_pool(self, lane, create=False):
        pool = self._reinforcements.get(lane)
        if pool is not None:
            return pool
        # JSON object keys are strings, so a lane originally keyed by an
        # integer comes back as ``"1"``.  Accept both spellings.
        alias = str(lane)
        pool = self._reinforcements.get(alias)
        if pool is not None:
            return pool
        if create:
            pool = {}
            self._reinforcements[lane] = pool
            return pool
        return {}

    def _settlement_for(self, round_id):
        settlement = self._settlements.get(round_id)
        if settlement is not None:
            return settlement
        # JSON converts object keys to strings.  Accept the original integer
        # round id after a snapshot has been decoded again.
        alias = str(round_id)
        settlement = self._settlements.get(alias)
        if settlement is not None:
            return settlement
        if isinstance(round_id, str):
            try:
                numeric = int(round_id)
            except ValueError:
                return None
            return self._settlements.get(numeric)
        return None

    def _state_locked(self, point_id):
        point = self._points.get(point_id)
        if point is None:
            raise KeyError(point_id)
        return {
            "point": point_id,
            "lane": point["lane"],
            "boundary": tuple(point["boundary"]),
            "owner": point["owner"],
            "progress": point["progress"],
            "contest": sorted(point["contest"], key=repr),
        }

    def _advance_tick_locked(self, tick_time):
        if self._in_truce(tick_time):
            return []

        captured = []
        for point_id, point in self._points.items():
            contest = point["contest"]
            owner = point["owner"]

            if len(contest) >= 2:
                # Simultaneous contest never shares or preserves a bar.
                point["progress"] = 0
                continue

            if not contest:
                if owner is not None:
                    self._scores[owner] = self._scores.get(owner, 0) + 1
                continue

            contender = next(iter(contest))
            if owner == contender:
                # Owned and uncontested: the current owner is also the sole
                # faction receiving the per-tick score.
                point["progress"] = 0
                self._scores[owner] = self._scores.get(owner, 0) + 1
                continue

            point["progress"] += 1
            if point["progress"] >= self.capture_progress:
                point["owner"] = contender
                point["progress"] = 0
                self._scores[contender] = (
                    self._scores.get(contender, 0) + self.capture_award
                )
                captured.append(
                    {"point": point_id, "faction": contender}
                )
        return captured
