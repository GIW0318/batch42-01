"""Tournament bracket seeding, byes, advancement and audit.

The implementation is intentionally in-memory.  ``snapshot``/``restore`` are
the persistence boundary and contain everything needed to rebuild a bracket
after a process restart.  A bracket version is monotonic and clients must
present the current version when they ask for a view.
"""

from __future__ import annotations

import copy
import functools
import re
import threading
from contextlib import contextmanager
from datetime import datetime, timezone


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


def _synchronized(method):
    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)
    return wrapper


class TournamentBracketSeeding:
    def __init__(self, size=8, server_time=0):
        if not isinstance(size, int) or size < 2 or size & (size - 1):
            raise ValueError("size must be a power of two greater than one")
        self.size = size
        self.server_time = server_time
        self._rounds = {}
        self._matches = {}
        self._history = {}
        self._conflicts = []
        self._rewards = []
        self._seeding_order = []
        self._entrants = []
        self._third_place = None
        self._version = 0
        self._next_match_seq = 1
        self._next_conflict_seq = 1
        self._started = False
        self._lock = threading.RLock()

    # ------------------------------------------------------------------
    # Basic bracket construction
    # ------------------------------------------------------------------

    @property
    def round_count(self):
        return self.size.bit_length() - 1

    @_synchronized
    def advance(self, seconds):
        self.server_time += seconds
        self._version += 1
        return self.server_time

    @_synchronized
    def seed(self, entrants):
        """Create a fresh bracket from entrants.

        Seeds are ranked first and then placed into a standard bracket order.
        Equal seeds are ordered by their registration instant in UTC, so an
        entrant registering at 12:00+08:00 sorts after one at 01:00+00:00.
        """

        normalised = []
        seen = set()
        for index, entrant in enumerate(entrants or []):
            if isinstance(entrant, str):
                entrant = {"player_id": entrant, "seed": index + 1}
            if not isinstance(entrant, dict):
                raise ValueError("entrants must be dictionaries")
            player_id = entrant.get("player_id")
            if player_id is None:
                raise ValueError("entrant is missing player_id")
            if player_id in seen:
                raise ValueError("duplicate player_id: %s" % player_id)
            seen.add(player_id)
            seed_value = entrant.get("seed", index + 1)
            try:
                seed_value = int(seed_value)
            except (TypeError, ValueError):
                seed_value = self.size + index + 1
            normalised.append({
                "player_id": player_id,
                "seed": seed_value,
                "registered_at": entrant.get("registered_at"),
                "_index": index,
            })

        if len(normalised) > self.size:
            raise ValueError("more entrants than bracket slots")

        self._rounds = {}
        self._matches = {}
        self._history = {}
        self._conflicts = []
        self._rewards = []
        self._third_place = None
        self._next_match_seq = 1
        self._next_conflict_seq = 1
        self._started = False

        normalised.sort(key=lambda item: (
            item["seed"],
            self._utc_timestamp(item["registered_at"]),
            item["_index"],
            str(item["player_id"]),
        ))
        self._entrants = copy.deepcopy(normalised)
        self._seeding_order = [item["player_id"] for item in normalised]

        bracket_order = self._bracket_order(self.size)
        placements = [None] * self.size
        for rank, entrant in enumerate(normalised, 1):
            position = bracket_order.index(rank)
            placements[position] = entrant["player_id"]

        for round_no in range(1, self.round_count + 1):
            match_count = self.size // (2 ** round_no)
            round_matches = []
            for index in range(match_count):
                match = self._new_match(round_no, index)
                if round_no == 1:
                    match["players"] = [
                        placements[index * 2],
                        placements[index * 2 + 1],
                    ]
                    match["status"] = self._initial_status(match["players"])
                    match["bye"] = match["status"] == "bye"
                    if match["bye"]:
                        match["bye_player"] = self._only_player(match["players"])
                        match["winner"] = match["bye_player"]
                round_matches.append(match)
            self._rounds[round_no] = round_matches

        for round_matches in self._rounds.values():
            for match in round_matches:
                self._matches[match["match_id"]] = match
                self._history.setdefault(match["match_id"], [])

        self._project_round_two()
        self._propagate_structure()
        for match in self._matches.values():
            if match["status"] == "bye" and not any(
                    entry.get("action") == "bye"
                    for entry in self._history[match["match_id"]]):
                self._history[match["match_id"]].append({
                    "action": "bye",
                    "player_id": match["bye_player"],
                    "round_no": match["round_no"],
                    "slot": match["slot"],
                    "at": self.server_time,
                })

        self._version += 1
        return self.view(self._version)

    @_synchronized
    def seeding_order(self):
        return list(self._seeding_order)

    @_synchronized
    def start(self):
        if not self._rounds:
            raise BracketError(NOT_STARTED, "bracket has not been seeded")
        changed = not self._started
        self._started = True
        for match in self._matches.values():
            if match.get("start_time") is None:
                match["start_time"] = self._default_start(match["round_no"])
                changed = True
        if changed:
            self._version += 1
        return self.view(self._version)

    # ------------------------------------------------------------------
    # Read APIs
    # ------------------------------------------------------------------

    @_synchronized
    def matches(self, round_no):
        try:
            round_no = int(round_no)
        except (TypeError, ValueError):
            return []
        return [copy.deepcopy(match)
                for match in self._rounds.get(round_no, [])]

    @_synchronized
    def third_place_match(self):
        if self._third_place is not None:
            return copy.deepcopy(self._third_place)
        if self.round_count < 2:
            raise BracketError(NOT_STARTED, "no semi-finals in this bracket")

        semi_final_round = self.round_count - 1
        semi_finals = self._rounds.get(semi_final_round, [])
        if len(semi_finals) != 2 or any(
                match["status"] not in ("finished", "archived")
                for match in semi_finals):
            raise BracketError(NOT_STARTED, "semi-finals are not finished")

        losers = [match.get("loser") for match in semi_finals]
        if any(loser is None for loser in losers):
            raise BracketError(NOT_STARTED, "semi-final losers are unknown")

        final_match = self._rounds.get(self.round_count, [None])[0]
        third = self._new_match(self.round_count, None)
        third["is_third_place"] = True
        third["slot"] = -1
        third["players"] = losers
        third["status"] = "pending"
        third["start_time"] = (
            final_match.get("start_time") if final_match else
            self._default_start(self.round_count)
        )
        self._third_place = third
        self._matches[third["match_id"]] = third
        self._history.setdefault(third["match_id"], [])
        self._history[third["match_id"]].append({
            "action": "created",
            "action_type": "third_place",
            "at": self.server_time,
        })
        self._version += 1
        return copy.deepcopy(third)

    @_synchronized
    def match(self, match_id):
        match = self._matches.get(match_id)
        if match is None:
            raise BracketError(MATCH_NOT_FOUND, str(match_id))
        return copy.deepcopy(match)

    @_synchronized
    def conflicts(self):
        return [
            copy.deepcopy(conflict)
            for conflict in self._conflicts
            if conflict.get("status") == "open"
        ]

    @_synchronized
    def slot_winner(self, round_no, slot):
        try:
            round_no = int(round_no)
            slot = int(slot)
        except (TypeError, ValueError):
            return None
        round_matches = self._rounds.get(round_no, [])
        if slot < 0 or slot // 2 >= len(round_matches):
            return None
        match = round_matches[slot // 2]
        if slot % 2 >= len(match["players"]):
            return None
        return match["players"][slot % 2]

    @_synchronized
    def match_history(self, match_id):
        if match_id not in self._matches:
            raise BracketError(MATCH_NOT_FOUND, str(match_id))
        return copy.deepcopy(self._history.get(match_id, []))

    @_synchronized
    def pending_rewards(self):
        return [
            copy.deepcopy(reward)
            for reward in self._rewards
            if reward.get("status") == "pending"
        ]

    @_synchronized
    def current_version(self):
        return self._version

    @_synchronized
    def view(self, version):
        if isinstance(version, dict):
            version = version.get("version")
        if version is None or int(version) != self._version:
            raise BracketError(STALE_BRACKET, "client view is stale")
        rounds = {
            str(round_no): [copy.deepcopy(match) for match in round_matches]
            for round_no, round_matches in sorted(self._rounds.items())
        }
        active_matches = [
            copy.deepcopy(match)
            for round_no in sorted(self._rounds)
            for match in self._rounds[round_no]
        ]
        if self._third_place is not None:
            active_matches.append(copy.deepcopy(self._third_place))
        return {
            "version": self._version,
            "size": self.size,
            "server_time": self.server_time,
            "started": self._started,
            "seeding_order": list(self._seeding_order),
            "rounds": rounds,
            "bracket": copy.deepcopy(rounds),
            "matches": active_matches,
            "third_place": (
                copy.deepcopy(self._third_place)
                if self._third_place is not None else None
            ),
            "conflicts": self.conflicts(),
        }

    # ------------------------------------------------------------------
    # Result submission and slot advancement
    # ------------------------------------------------------------------

    @_synchronized
    def report_result(self, match_id, score, operator="ref"):
        match = self._require_match(match_id)
        status = match["status"]

        if status == "bye":
            raise BracketError(BYE_MATCH, "%s is a bye" % match_id)
        if status == "archived":
            return self._result("advanced", match_id,
                                winner=match.get("winner"))
        if status == "empty":
            raise BracketError(NOT_STARTED, "%s has no players" % match_id)
        if any(player is None for player in match["players"]):
            raise BracketError(NOT_STARTED, "%s is not ready" % match_id)
        if match.get("provisional"):
            raise BracketError(NOT_STARTED, "%s still has a projected player"
                               % match_id)

        if status == "finished":
            if score == match.get("score"):
                return self._result("advanced", match_id,
                                    winner=match.get("winner"))
            conflict = self._record_conflict(
                match=match,
                candidate=self._winner_from_score(match, score),
                operator=operator,
                score=score,
            )
            self._history[match_id].append({
                "action": "result_rejected",
                "score": score,
                "operator": operator,
                "winner": conflict.get("winner"),
                "at": self.server_time,
            })
            self._version += 1
            return {
                "status": "conflict",
                "match_id": match_id,
                "conflict_id": conflict["conflict_id"],
            }

        winner = self._winner_from_score(match, score)
        loser = self._other_player(match["players"], winner)
        match["status"] = "finished"
        match["score"] = copy.deepcopy(score)
        match["winner"] = winner
        match["loser"] = loser
        self._history[match_id].append({
            "action": "result",
            "score": copy.deepcopy(score),
            "operator": operator,
            "winner": winner,
            "at": self.server_time,
        })
        self._add_reward(winner, match_id, "win")

        conflict = self._advance_match(match, winner, operator)
        self._propagate_structure()
        self._version += 1
        if conflict is not None:
            return {
                "status": "conflict",
                "match_id": match_id,
                "conflict_id": conflict["conflict_id"],
            }
        return {
            "status": "recorded",
            "match_id": match_id,
            "winner": winner,
        }

    @_synchronized
    def commit_slot(self, round_no, slot, match_id, winner, operator="ref"):
        source_match = self._matches.get(match_id)
        if source_match is None:
            raise BracketError(MATCH_NOT_FOUND, str(match_id))
        if winner not in source_match["players"]:
            raise BracketError(SLOT_CONFLICT, "winner is not in source match")

        try:
            round_no = int(round_no)
            slot = int(slot)
        except (TypeError, ValueError):
            raise BracketError(SLOT_CONFLICT, "invalid slot")

        destination = self._destination_match(round_no, slot)
        if destination is None:
            raise BracketError(SLOT_CONFLICT, "invalid slot")
        if destination["status"] in ("finished", "archived"):
            conflict = self._record_conflict(
                match=source_match,
                candidate=winner,
                operator=operator,
                round_no=round_no,
                slot=slot,
                existing_winner=self.slot_winner(round_no, slot),
            )
            self._version += 1
            return {
                "status": "conflict",
                "match_id": match_id,
                "conflict_id": conflict["conflict_id"],
            }

        position = slot % 2
        existing = destination["players"][position]
        provisional = destination.setdefault("provisional", [])
        if position in provisional:
            destination["players"][position] = winner
            provisional.remove(position)
            self._propagate_structure()
            self._version += 1
            return {
                "status": "advanced",
                "match_id": match_id,
                "round_no": round_no,
                "slot": slot,
                "winner": winner,
            }
        if existing is None:
            destination["players"][position] = winner
            self._propagate_structure()
            self._version += 1
            return {
                "status": "advanced",
                "match_id": match_id,
                "round_no": round_no,
                "slot": slot,
                "winner": winner,
            }
        if existing == winner:
            return {
                "status": "advanced",
                "match_id": match_id,
                "round_no": round_no,
                "slot": slot,
                "winner": winner,
            }

        conflict = self._record_conflict(
            match=source_match,
            candidate=winner,
            operator=operator,
            round_no=round_no,
            slot=slot,
            existing_winner=existing,
        )
        self._version += 1
        return {
            "status": "conflict",
            "match_id": match_id,
            "conflict_id": conflict["conflict_id"],
            "existing_winner": existing,
        }

    @_synchronized
    def forfeit(self, match_id, player_id, operator="ref"):
        match = self._require_match(match_id)
        status = match["status"]

        if status == "bye":
            raise BracketError(BYE_MATCH, "%s is a bye" % match_id)
        if status == "archived":
            return self._result("advanced", match_id,
                                winner=match.get("winner"))
        if status == "empty":
            raise BracketError(NOT_STARTED, "%s has no players" % match_id)
        if status == "finished":
            if match.get("forfeit") == player_id:
                return self._result("advanced", match_id,
                                    winner=match.get("winner"))
            conflict = self._record_conflict(
                match=match,
                candidate=player_id,
                operator=operator,
                existing_winner=match.get("winner"),
            )
            self._version += 1
            return {
                "status": "conflict",
                "match_id": match_id,
                "conflict_id": conflict["conflict_id"],
            }
        if player_id not in match["players"] or player_id is None:
            raise BracketError(SLOT_CONFLICT, "player is not in match")
        if any(player is None for player in match["players"]):
            raise BracketError(NOT_STARTED, "%s is not ready" % match_id)
        if match.get("provisional"):
            raise BracketError(NOT_STARTED, "%s still has a projected player"
                               % match_id)

        opponent = self._other_player(match["players"], player_id)

        # The match state, the reward cancellation and the advancement are
        # committed together.  Any unexpected exception rolls all of them
        # back to the pre-forfeit state.
        with self._atomic():
            match["status"] = "finished"
            match["score"] = "forfeit"
            match["winner"] = opponent
            match["loser"] = player_id
            match["forfeit"] = player_id
            self._history[match_id].append({
                "action": "forfeit",
                "player_id": player_id,
                "operator": operator,
                "winner": opponent,
                "at": self.server_time,
            })
            self._cancel_rewards(player_id, match_id, operator)
            self._add_reward(opponent, match_id, "forfeit_advance")
            conflict = self._advance_match(match, opponent, operator)
            self._propagate_structure()
            self._version += 1

        if conflict is not None:
            return {
                "status": "conflict",
                "match_id": match_id,
                "conflict_id": conflict["conflict_id"],
                "winner": opponent,
            }
        return {
            "status": "forfeit",
            "match_id": match_id,
            "winner": opponent,
            "forfeited": player_id,
        }

    # ------------------------------------------------------------------
    # Scheduling and replay
    # ------------------------------------------------------------------

    @_synchronized
    def reschedule(self, match_id, new_start, operator="ref"):
        match = self._require_match(match_id)
        if match["status"] in ("finished", "archived"):
            raise BracketError(MATCH_FINISHED, "%s is already finished" % match_id)
        if match["status"] == "bye":
            raise BracketError(BYE_MATCH, "%s is a bye" % match_id)
        if match["status"] == "empty":
            raise BracketError(NOT_STARTED, "%s has no players" % match_id)

        old_start = match.get("start_time")
        match["start_time"] = new_start
        self._history[match_id].append({
            "action": "reschedule",
            "operator": operator,
            "old_start": old_start,
            "new_start": new_start,
            "at": self.server_time,
        })
        self._version += 1
        return {
            "status": "rescheduled",
            "match_id": match_id,
            "old_start": old_start,
            "new_start": new_start,
        }

    @_synchronized
    def replay(self, match_id):
        old_match = self._require_match(match_id)
        if old_match["status"] in ("bye", "empty"):
            raise BracketError(MATCH_FINISHED, "%s cannot be replayed" % match_id)
        if old_match["status"] != "finished":
            raise BracketError(NOT_STARTED, "%s has not finished" % match_id)

        if old_match.get("is_third_place"):
            replay_match = self._new_match(self.round_count, None)
            replay_match["is_third_place"] = True
            replay_match["slot"] = -1
            self._third_place = replay_match
        else:
            replay_match = self._new_match(
                old_match["round_no"], old_match["slot"])

        replay_match["players"] = list(old_match["players"])
        replay_match["status"] = "pending"
        replay_match["start_time"] = old_match.get("start_time")
        replay_match["replay_of"] = match_id
        replay_match["is_third_place"] = bool(old_match.get("is_third_place"))

        old_match["status"] = "archived"
        self._history[match_id].append({
            "action": "replay",
            "new_match_id": replay_match["match_id"],
            "at": self.server_time,
        })

        if not old_match.get("is_third_place"):
            self._retract_advancement(old_match)
            round_matches = self._rounds[old_match["round_no"]]
            round_matches[old_match["slot"]] = replay_match

        self._matches[replay_match["match_id"]] = replay_match
        self._history.setdefault(replay_match["match_id"], [])
        self._propagate_structure()
        self._version += 1
        return copy.deepcopy(replay_match)

    @_synchronized
    def resolve_conflict(self, conflict_id, winner, operator="ref"):
        conflict = None
        for candidate in self._conflicts:
            if (candidate.get("conflict_id") == conflict_id and
                    candidate.get("status") == "open"):
                conflict = candidate
                break
        if conflict is None:
            raise BracketError(MATCH_NOT_FOUND, str(conflict_id))

        allowed = {conflict.get("existing_winner"), conflict.get("winner")}
        allowed.discard(None)
        if winner not in allowed:
            raise BracketError(SLOT_CONFLICT, "winner is not a conflict candidate")

        round_no = conflict.get("round_no")
        slot = conflict.get("slot")
        if round_no is not None and slot is not None and slot >= 0:
            destination = self._destination_match(round_no, slot)
            if destination is None:
                raise BracketError(SLOT_CONFLICT, "conflict slot no longer exists")
            if destination["status"] in ("finished", "archived"):
                raise BracketError(MATCH_FINISHED, "destination is already finished")
            destination["players"][slot % 2] = winner
            self._propagate_structure()

        conflict["status"] = "resolved"
        conflict["resolved_winner"] = winner
        conflict["resolved_by"] = operator
        conflict["resolved_at"] = self.server_time
        self._history.setdefault(conflict.get("match_id"), []).append({
            "action": "conflict_resolved",
            "conflict_id": conflict_id,
            "winner": winner,
            "operator": operator,
            "at": self.server_time,
        })
        self._version += 1
        return copy.deepcopy(conflict)

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    @_synchronized
    def snapshot(self):
        active_ids = {
            match["match_id"]
            for round_matches in self._rounds.values()
            for match in round_matches
        }
        if self._third_place is not None:
            active_ids.add(self._third_place["match_id"])
        archived = [
            copy.deepcopy(match)
            for match_id, match in self._matches.items()
            if match_id not in active_ids
        ]
        return {
            "format": 1,
            "version": self._version,
            "size": self.size,
            "server_time": self.server_time,
            "started": self._started,
            "entrants": copy.deepcopy(self._entrants),
            "seeding_order": list(self._seeding_order),
            "rounds": {
                str(round_no): [copy.deepcopy(match) for match in round_matches]
                for round_no, round_matches in sorted(self._rounds.items())
            },
            "third_place": (
                copy.deepcopy(self._third_place)
                if self._third_place is not None else None
            ),
            "archived_matches": archived,
            "history": copy.deepcopy(self._history),
            "conflicts": copy.deepcopy(self._conflicts),
            "rewards": copy.deepcopy(self._rewards),
            "next_match_seq": self._next_match_seq,
            "next_conflict_seq": self._next_conflict_seq,
        }

    @_synchronized
    def restore(self, snapshot):
        if not isinstance(snapshot, dict):
            raise BracketError("invalid_snapshot", "snapshot must be a dictionary")
        incoming_version = self._snapshot_version(snapshot)

        # An old snapshot must never resurrect a finished match.  A restarted,
        # empty instance can always adopt the snapshot; a live instance ignores
        # snapshots older than its current state.
        if self._matches and (
                incoming_version < self._version or
                self._snapshot_would_revive(snapshot)):
            return self.view(self._version)

        try:
            size = int(snapshot.get("size", self.size))
        except (TypeError, ValueError):
            raise BracketError("invalid_snapshot", "invalid bracket size")
        if size < 2 or size & (size - 1):
            raise BracketError("invalid_snapshot", "invalid bracket size")

        self.size = size
        self.server_time = snapshot.get("server_time", 0)
        self._started = bool(snapshot.get("started", False))
        self._entrants = copy.deepcopy(snapshot.get("entrants", []))
        self._seeding_order = list(snapshot.get("seeding_order", []))
        self._history = copy.deepcopy(snapshot.get("history", {}))
        self._conflicts = copy.deepcopy(snapshot.get("conflicts", []))
        self._rewards = copy.deepcopy(snapshot.get("rewards", []))
        self._version = incoming_version
        self._next_match_seq = int(snapshot.get("next_match_seq", 1))
        self._next_conflict_seq = int(snapshot.get("next_conflict_seq", 1))

        self._rounds = {}
        self._matches = {}
        for round_key, round_matches in snapshot.get("rounds", {}).items():
            round_no = int(round_key)
            restored_round = []
            for match in copy.deepcopy(round_matches):
                restored_round.append(match)
                self._matches[match["match_id"]] = match
                self._history.setdefault(match["match_id"], [])
            self._rounds[round_no] = restored_round

        third = snapshot.get("third_place")
        self._third_place = copy.deepcopy(third) if third is not None else None
        if self._third_place is not None:
            self._matches[self._third_place["match_id"]] = self._third_place
            self._history.setdefault(self._third_place["match_id"], [])

        for match in copy.deepcopy(snapshot.get("archived_matches", [])):
            self._matches[match["match_id"]] = match
            self._history.setdefault(match["match_id"], [])

        self._normalise_restored_state()
        return self.view(self._version)

    @_synchronized
    def recover(self, snapshot=None):
        """Rebuild indexes after a restart and return a fresh server view."""

        if snapshot is not None:
            self.restore(snapshot)
        known = {}
        for round_no in sorted(self._rounds):
            for match in self._rounds[round_no]:
                known[match["match_id"]] = match
        if self._third_place is not None:
            known[self._third_place["match_id"]] = self._third_place
        for match_id, match in self._matches.items():
            known.setdefault(match_id, match)
        self._matches = known
        for match in self._matches.values():
            self._history.setdefault(match["match_id"], [])
        self._next_match_seq = max(
            self._next_match_seq, self._next_sequence(self._matches) + 1)
        self._next_conflict_seq = max(
            self._next_conflict_seq,
            self._next_conflict_sequence(self._conflicts) + 1,
        )
        self._propagate_structure()
        return self.view(self._version)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _new_match(self, round_no, index):
        match_id = "m%d" % self._next_match_seq
        self._next_match_seq += 1
        return {
            "match_id": match_id,
            "round_no": round_no,
            "slot": index,
            "index": index,
            "players": [None, None],
            "status": "pending",
            "score": None,
            "winner": None,
            "loser": None,
            "start_time": self._default_start(round_no),
            "bye": False,
            "bye_player": None,
            "forfeit": None,
            "replay_of": None,
            "is_third_place": False,
            "provisional": [],
            "next_round": (
                round_no + 1
                if round_no is not None and round_no < self.round_count
                else None
            ),
            "next_slot": (
                index if round_no is not None and round_no < self.round_count
                else None
            ),
        }

    def _default_start(self, round_no):
        if round_no is None:
            return self.server_time
        return self.server_time + (round_no - 1) * 3600

    def _require_match(self, match_id):
        match = self._matches.get(match_id)
        if match is None:
            raise BracketError(MATCH_NOT_FOUND, str(match_id))
        return match

    @staticmethod
    def _result(status, match_id, **extra):
        if status == "advanced":
            status = BRACKET_ADVANCED
        result = {"status": status, "match_id": match_id}
        result.update(extra)
        return result

    @staticmethod
    def _initial_status(players):
        present = [player for player in players if player is not None]
        if len(present) == 2:
            return "pending"
        if len(present) == 1:
            return "bye"
        return "empty"

    @staticmethod
    def _only_player(players):
        for player in players:
            if player is not None:
                return player
        return None

    @staticmethod
    def _other_player(players, player):
        for candidate in players:
            if candidate is not None and candidate != player:
                return candidate
        return None

    @staticmethod
    def _bracket_order(size):
        order = [1]
        while len(order) < size:
            next_size = len(order) * 2
            updated = []
            for seed in order:
                updated.extend([seed, next_size + 1 - seed])
            order = updated
        return order

    def _project_round_two(self):
        """Show the higher seed as the provisional occupant of a bye draw.

        A bye-heavy draw has fewer than ``size / 4`` known second-round
        players until the preliminary matches are played.  The bracket view
        keeps the seeded slot occupied provisionally; a real result replaces
        that projection without creating a false slot conflict.
        """

        if self.round_count < 2:
            return
        first_round = self._rounds.get(1, [])
        if not any(match["status"] == "bye" for match in first_round):
            return
        rank = {
            player_id: index
            for index, player_id in enumerate(self._seeding_order)
        }
        for match in first_round:
            if match["status"] != "pending":
                continue
            players = [player for player in match["players"] if player]
            if len(players) != 2:
                continue
            candidate = min(players, key=lambda player: rank.get(
                player, len(rank)))
            destination = self._destination_match(2, match["index"])
            if destination is None:
                continue
            position = match["index"] % 2
            if destination["players"][position] is None:
                destination["players"][position] = candidate
                destination.setdefault("provisional", []).append(position)

    @staticmethod
    def _utc_timestamp(value):
        if value is None:
            return float("inf")
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, datetime):
            parsed = value
        else:
            text = str(value).strip()
            if text.endswith("Z"):
                text = text[:-1] + "+00:00"
            try:
                parsed = datetime.fromisoformat(text)
            except ValueError:
                return float("inf")
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()

    def _winner_from_score(self, match, score):
        first, second = match["players"][0], match["players"][1]
        values = []
        if isinstance(score, dict):
            values = list(score.values())[:2]
        elif isinstance(score, (list, tuple)):
            values = list(score)[:2]
        elif isinstance(score, (int, float)):
            values = [score]
        else:
            numbers = re.findall(r"\d+(?:\.\d+)?", str(score))
            values = numbers[:2]
        try:
            left = float(values[0])
            right = float(values[1]) if len(values) > 1 else 0.0
        except (IndexError, TypeError, ValueError):
            return first
        return first if left >= right else second

    def _destination_match(self, round_no, slot):
        try:
            round_no = int(round_no)
            slot = int(slot)
        except (TypeError, ValueError):
            return None
        round_matches = self._rounds.get(round_no, [])
        if slot < 0:
            return None
        match_index = slot // 2
        if match_index >= len(round_matches):
            return None
        return round_matches[match_index]

    def _advance_match(self, match, winner, operator):
        if match.get("next_round") is None:
            return None
        destination = self._destination_match(
            match["next_round"], match["next_slot"])
        if destination is None:
            return None
        position = match["next_slot"] % 2
        existing = destination["players"][position]
        provisional = destination.setdefault("provisional", [])
        if position in provisional:
            destination["players"][position] = winner
            provisional.remove(position)
            return None
        if existing is None:
            destination["players"][position] = winner
            return None
        if existing == winner:
            return None
        conflict = self._record_conflict(
            match=match,
            candidate=winner,
            operator=operator,
            round_no=match["next_round"],
            slot=match["next_slot"],
            existing_winner=existing,
        )
        self._history[match["match_id"]].append({
            "action": "advance_conflict",
            "conflict_id": conflict["conflict_id"],
            "winner": winner,
            "existing_winner": existing,
            "operator": operator,
            "at": self.server_time,
        })
        return conflict

    def _retract_advancement(self, match):
        if match.get("next_round") is None or match.get("winner") is None:
            return
        destination = self._destination_match(
            match["next_round"], match["next_slot"])
        if destination is None or destination["status"] in ("finished", "archived"):
            return
        position = match["next_slot"] % 2
        if destination["players"][position] == match["winner"]:
            destination["players"][position] = None

    def _record_conflict(self, match, candidate, operator, score=None,
                         round_no=None, slot=None, existing_winner=None):
        if round_no is None and match.get("next_round") is not None:
            round_no = match["next_round"]
            slot = match["next_slot"]
        if slot is None:
            slot = match.get("slot")
        if existing_winner is None and round_no is not None and slot is not None:
            existing_winner = self.slot_winner(round_no, slot)

        for conflict in self._conflicts:
            if (conflict.get("status") == "open" and
                    conflict.get("match_id") == match.get("match_id") and
                    conflict.get("round_no") == round_no and
                    conflict.get("slot") == slot and
                    conflict.get("winner") == candidate):
                return conflict

        conflict = {
            "conflict_id": "c%d" % self._next_conflict_seq,
            "id": "c%d" % self._next_conflict_seq,
            "round_no": round_no,
            "slot": slot,
            "match_id": match.get("match_id"),
            "winner": candidate,
            "existing_winner": existing_winner,
            "operator": operator,
            "score": copy.deepcopy(score),
            "status": "open",
            "created_at": self.server_time,
        }
        self._next_conflict_seq += 1
        self._conflicts.append(conflict)
        return conflict

    def _propagate_structure(self):
        for round_no in range(2, self.round_count + 1):
            for match in self._rounds.get(round_no, []):
                if match["status"] in ("finished", "archived"):
                    continue
                previous_round = self._rounds.get(round_no - 1, [])
                known = []
                unknown_count = 0
                for position in range(2):
                    source_index = match["index"] * 2 + position
                    if source_index >= len(previous_round):
                        continue
                    source = previous_round[source_index]
                    existing = match["players"][position]
                    if existing is not None:
                        known.append(existing)
                        continue
                    source_status = source["status"]
                    if source_status == "bye":
                        player = source.get("bye_player") or source.get("winner")
                        if player is not None:
                            match["players"][position] = player
                            known.append(player)
                    elif source_status == "empty":
                        continue
                    elif source_status == "finished":
                        if source.get("winner") is not None:
                            match["players"][position] = source["winner"]
                            known.append(source["winner"])
                    elif source_status == "archived":
                        if source.get("winner") is not None:
                            match["players"][position] = source["winner"]
                            known.append(source["winner"])
                    else:
                        unknown_count += 1

                previous_status = match["status"]
                if len(known) == 2:
                    match["status"] = "pending"
                    match["bye"] = False
                    match["bye_player"] = None
                elif len(known) == 1 and unknown_count == 0:
                    match["status"] = "bye"
                    match["bye"] = True
                    match["bye_player"] = known[0]
                    match["winner"] = known[0]
                elif len(known) == 0 and unknown_count == 0:
                    match["status"] = "empty"
                    match["bye"] = False
                    match["bye_player"] = None
                    match["winner"] = None
                else:
                    match["status"] = "pending"
                    match["bye"] = False
                    match["bye_player"] = None

                if match["status"] == "bye" and match.get("provisional"):
                    match["status"] = "pending"
                    match["bye"] = False
                    match["bye_player"] = None

                if (match["status"] == "bye" and
                        previous_status != "bye"):
                    self._history.setdefault(match["match_id"], []).append({
                        "action": "bye",
                        "player_id": match["bye_player"],
                        "round_no": match["round_no"],
                        "slot": match["slot"],
                        "at": self.server_time,
                    })

    def _add_reward(self, player_id, match_id, reason):
        if player_id is None:
            return
        for reward in self._rewards:
            if (reward.get("status") == "pending" and
                    reward.get("player_id") == player_id and
                    reward.get("match_id") == match_id):
                return
        self._rewards.append({
            "player_id": player_id,
            "match_id": match_id,
            "reason": reason,
            "status": "pending",
            "created_at": self.server_time,
        })

    def _cancel_rewards(self, player_id, match_id, operator):
        found_for_match = False
        for reward in self._rewards:
            if (reward.get("status") == "pending" and
                    reward.get("player_id") == player_id):
                reward["status"] = "cancelled"
                reward["cancelled_at"] = self.server_time
                reward["cancelled_by"] = operator
                reward["cancel_reason"] = "forfeit"
                reward.setdefault("forfeit_match_id", match_id)
        for reward in self._rewards:
            if (reward.get("player_id") == player_id and
                    reward.get("match_id") == match_id):
                found_for_match = True
                break
        if not found_for_match:
            self._rewards.append({
                "player_id": player_id,
                "match_id": match_id,
                "reason": "win",
                "status": "cancelled",
                "created_at": self.server_time,
                "cancelled_at": self.server_time,
                "cancelled_by": operator,
                "cancel_reason": "forfeit",
                "forfeit_match_id": match_id,
            })

    @contextmanager
    def _atomic(self):
        state = copy.deepcopy({
            key: value
            for key, value in self.__dict__.items()
            if key != "_lock"
        })
        try:
            yield
        except Exception:
            self.__dict__.clear()
            self.__dict__.update(state)
            self.__dict__["_lock"] = threading.RLock()
            raise

    def _normalise_restored_state(self):
        self._next_match_seq = max(
            self._next_match_seq, self._next_sequence(self._matches) + 1)
        self._next_conflict_seq = max(
            self._next_conflict_seq,
            self._next_conflict_sequence(self._conflicts) + 1,
        )
        self._propagate_structure()

    @staticmethod
    def _snapshot_version(snapshot):
        try:
            return int(snapshot.get("version", 0))
        except (TypeError, ValueError):
            return 0

    def _snapshot_would_revive(self, snapshot):
        incoming = {}
        for round_matches in snapshot.get("rounds", {}).values():
            for match in round_matches:
                incoming[match.get("match_id")] = match
        if snapshot.get("third_place") is not None:
            third = snapshot["third_place"]
            incoming[third.get("match_id")] = third
        for match in snapshot.get("archived_matches", []):
            incoming[match.get("match_id")] = match
        for match_id, current in self._matches.items():
            if current.get("status") not in ("finished", "archived"):
                continue
            restored = incoming.get(match_id)
            if restored is None or restored.get("status") not in (
                    "finished", "archived"):
                return True
        return False

    @staticmethod
    def _next_sequence(matches):
        highest = 0
        for match_id in matches:
            found = re.search(r"(\d+)$", str(match_id))
            if found:
                highest = max(highest, int(found.group(1)))
        return highest

    @staticmethod
    def _next_conflict_sequence(conflicts):
        highest = 0
        for conflict in conflicts:
            found = re.search(r"(\d+)$", str(conflict.get("conflict_id", "")))
            if found:
                highest = max(highest, int(found.group(1)))
        return highest
