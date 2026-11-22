"""Ranked draft pick/ban phase, swaps and reconnect handling.

The draft state machine is deliberately server authoritative.  Callers can
only move time forward with :meth:`RankedDraftPick.advance`; every operation
refreshes the match against that clock before touching draft state.
"""

from __future__ import annotations

import copy
import threading
from dataclasses import dataclass, field


CHAMPION_TAKEN = "champion_taken"
UNKNOWN_CHAMPION = "unknown_champion"
WRONG_PHASE = "wrong_phase"
ALREADY_PICKED = "already_picked"
DRAFT_INCOMPLETE = "draft_incomplete"
GAME_STARTED = "game_started"
ALREADY_RELEASED = "already_released"
PERSIST_FAILED = "persist_failed"
SWAP_CONFLICT = "swap_conflict"

# Additional failures used by the implementation.  Keeping them as draft
# errors makes malformed cross-match/client requests fail closed.
MATCH_NOT_FOUND = "match_not_found"
MATCH_EXISTS = "match_exists"
UNKNOWN_PLAYER = "unknown_player"
INVALID_SNAPSHOT = "invalid_snapshot"

_ACTIVE_PHASES = ("ban", "pick")
_PHASE_ORDER = {"ban": 0, "pick": 1, "complete": 2, "started": 3}


class DraftError(Exception):
    def __init__(self, code, message=""):
        self.code = code
        super().__init__(code if not message else "%s: %s" % (code, message))


@dataclass
class _Match:
    match_id: str
    blue_team: list
    red_team: list
    champion_pool: list
    phase: str = "ban"
    deadline: float = 0
    paused: bool = False
    paused_remaining: float | None = None
    bans: list = field(default_factory=list)
    picks: dict = field(default_factory=dict)
    actions: list = field(default_factory=list)
    pending_swaps: dict = field(default_factory=dict)
    open_seats: int = 0
    released: bool = False
    started: bool = False
    next_action_sequence: int = 1

    @property
    def players(self):
        return list(self.blue_team) + list(self.red_team)


class RankedDraftPick:
    def __init__(self, server_time=0, ban_seconds=30, pick_seconds=30,
                 champion_pool=()):
        self.server_time = float(server_time)
        self.ban_seconds = float(ban_seconds)
        self.pick_seconds = float(pick_seconds)
        self.champion_pool = list(champion_pool)
        self._matches = {}
        self._swap_sequence = 0
        self._lock = threading.RLock()

    # ------------------------------------------------------------------
    # Clock and phase transitions
    # ------------------------------------------------------------------
    def advance(self, seconds):
        with self._lock:
            seconds = float(seconds)
            if seconds < 0:
                raise ValueError("server time cannot move backwards")
            self.server_time += seconds
            for match in self._matches.values():
                self._refresh_match(match)
            return self.server_time

    def now(self):
        with self._lock:
            return self.server_time

    def _remaining(self, match):
        if match.paused:
            return max(0.0, float(match.paused_remaining or 0.0))
        if match.phase not in _ACTIVE_PHASES:
            return 0.0
        return max(0.0, match.deadline - self.server_time)

    def _refresh_match(self, match):
        """Advance a match to the phase implied by the server clock.

        Transitions carry the server-clock overshoot into the next phase.  In
        particular, a late call into ``ban`` cannot restart or extend its
        timer after the phase has already expired.
        """
        if match.started or match.phase not in _ACTIVE_PHASES or match.paused:
            return

        while match.phase in _ACTIVE_PHASES and \
                self.server_time >= match.deadline:
            if match.phase == "ban":
                match.phase = "pick"
                match.deadline += self.pick_seconds
                self._clear_pending_swaps(match)
            elif match.phase == "pick":
                self._auto_pick_remaining(match)
                match.phase = "complete"
                match.deadline = self.server_time
                self._clear_pending_swaps(match)

    def _require_match(self, match_id):
        match = self._matches.get(match_id)
        if match is None:
            raise DraftError(MATCH_NOT_FOUND)
        return match

    def _require_active(self, match, phase=None):
        if match.started:
            raise DraftError(GAME_STARTED)
        if match.released:
            raise DraftError(WRONG_PHASE)
        if match.paused:
            raise DraftError(WRONG_PHASE)
        if phase is not None and match.phase != phase:
            raise DraftError(WRONG_PHASE)
        if match.phase not in _ACTIVE_PHASES:
            raise DraftError(WRONG_PHASE)

    def _require_player(self, match, player_id):
        if player_id not in match.picks:
            raise DraftError(UNKNOWN_PLAYER)

    def _record_action(self, match, action_type, player_id, champion,
                       automatic=False):
        action = {
            "sequence": match.next_action_sequence,
            "type": action_type,
            "phase": match.phase,
            "player_id": player_id,
            "champion": champion,
        }
        if automatic:
            action["automatic"] = True
        match.next_action_sequence += 1
        match.actions.append(action)
        return action

    def _used_champions(self, match):
        return {entry["champion"] for entry in match.bans} | {
            champion for champion in match.picks.values()
            if champion is not None
        }

    def _validate_champion(self, match, champion):
        if champion not in match.champion_pool:
            raise DraftError(UNKNOWN_CHAMPION)
        if champion in self._used_champions(match):
            raise DraftError(CHAMPION_TAKEN)

    def _clear_pending_swaps(self, match):
        match.pending_swaps.clear()

    def _auto_pick_remaining(self, match):
        used = self._used_champions(match)
        for player_id in match.players:
            if match.picks.get(player_id) is not None:
                continue
            champion = next(
                (candidate for candidate in match.champion_pool
                 if candidate not in used),
                None,
            )
            if champion is None:
                break
            match.picks[player_id] = champion
            used.add(champion)
            self._record_action(
                match, "pick", player_id, champion, automatic=True
            )

    # ------------------------------------------------------------------
    # Draft lifecycle
    # ------------------------------------------------------------------
    def start_draft(self, match_id, blue_team, red_team, champion_pool=None):
        with self._lock:
            if match_id in self._matches:
                raise DraftError(MATCH_EXISTS)
            blue = list(blue_team)
            red = list(red_team)
            if len(set(blue + red)) != len(blue) + len(red):
                raise DraftError(UNKNOWN_PLAYER)

            pool = list(
                self.champion_pool if champion_pool is None else champion_pool
            )
            picks = {player_id: None for player_id in blue + red}
            match = _Match(
                match_id=match_id,
                blue_team=blue,
                red_team=red,
                champion_pool=pool,
                phase="ban",
                deadline=self.server_time + self.ban_seconds,
                picks=picks,
                open_seats=len(blue) + len(red),
            )
            self._matches[match_id] = match
            return self.phase_state(match_id)

    def phase_state(self, match_id):
        with self._lock:
            match = self._require_match(match_id)
            self._refresh_match(match)
            return {
                "match_id": match.match_id,
                "phase": match.phase,
                "remaining": self._remaining(match),
                "server_time": self.server_time,
                "paused": match.paused,
                "bans": [entry["champion"] for entry in match.bans],
                "picks": {
                    player_id: champion
                    for player_id, champion in match.picks.items()
                    if champion is not None
                },
                "actions": copy.deepcopy(match.actions),
                "pending_swaps": self._pending_swap_values(match),
                "open_seats": match.open_seats,
                "released": match.released,
                "started": match.started,
            }

    def ban(self, match_id, player_id, champion):
        with self._lock:
            match = self._require_match(match_id)
            self._refresh_match(match)
            self._require_active(match, "ban")
            self._require_player(match, player_id)
            self._validate_champion(match, champion)

            match.bans.append({
                "player_id": player_id,
                "champion": champion,
            })
            self._record_action(match, "ban", player_id, champion)
            return self.phase_state(match_id)

    def pick(self, match_id, player_id, champion):
        with self._lock:
            match = self._require_match(match_id)
            self._refresh_match(match)
            self._require_active(match, "pick")
            self._require_player(match, player_id)
            if match.picks.get(player_id) is not None:
                raise DraftError(ALREADY_PICKED)
            self._validate_champion(match, champion)

            match.picks[player_id] = champion
            self._record_action(match, "pick", player_id, champion)
            return self.phase_state(match_id)

    def advance_phase(self, match_id):
        with self._lock:
            match = self._require_match(match_id)
            self._refresh_match(match)
            if match.started:
                raise DraftError(GAME_STARTED)
            if match.released:
                raise DraftError(WRONG_PHASE)
            if match.paused:
                raise DraftError(WRONG_PHASE)
            if match.phase == "ban":
                match.phase = "pick"
                match.deadline = self.server_time + self.pick_seconds
            elif match.phase == "pick":
                match.phase = "complete"
                match.deadline = self.server_time
            else:
                raise DraftError(WRONG_PHASE)
            match.paused = False
            match.paused_remaining = None
            self._clear_pending_swaps(match)
            return self.phase_state(match_id)

    def pause(self, match_id):
        with self._lock:
            match = self._require_match(match_id)
            self._refresh_match(match)
            if match.paused:
                return self.phase_state(match_id)
            self._require_active(match)
            match.paused_remaining = self._remaining(match)
            match.paused = True
            return self.phase_state(match_id)

    def resume(self, match_id):
        with self._lock:
            match = self._require_match(match_id)
            if match.started:
                raise DraftError(GAME_STARTED)
            if match.phase not in _ACTIVE_PHASES:
                raise DraftError(WRONG_PHASE)
            if match.paused:
                remaining = max(0.0, float(match.paused_remaining or 0.0))
                match.deadline = self.server_time + remaining
                match.paused = False
                match.paused_remaining = None
            self._refresh_match(match)
            return self.phase_state(match_id)

    # Kept as explicit aliases for callers that use draft-specific names.
    pause_draft = pause
    resume_draft = resume

    def lineup(self, match_id):
        with self._lock:
            match = self._require_match(match_id)
            self._refresh_match(match)
            return dict(match.picks)

    # ------------------------------------------------------------------
    # Seats and reconnect
    # ------------------------------------------------------------------
    def open_seats(self, match_id):
        with self._lock:
            match = self._require_match(match_id)
            return match.open_seats

    def dodge(self, match_id, player_id):
        with self._lock:
            match = self._require_match(match_id)
            self._refresh_match(match)
            if match.started:
                raise DraftError(GAME_STARTED)
            if match.released:
                return {
                    "match_id": match_id,
                    "status": ALREADY_RELEASED,
                    "open_seats": match.open_seats,
                }
            self._require_player(match, player_id)
            match.released = True
            match.open_seats = 0
            self._clear_pending_swaps(match)
            return {
                "match_id": match_id,
                "status": "dodged",
                "open_seats": match.open_seats,
            }

    def reconnect(self, match_id, player_id):
        with self._lock:
            match = self._require_match(match_id)
            self._refresh_match(match)
            self._require_player(match, player_id)
            state = self.phase_state(match_id)
            state.update({
                "player_id": player_id,
                "lineup": dict(match.picks),
                "champion_pool": list(match.champion_pool),
            })
            return state

    # ------------------------------------------------------------------
    # Champion swaps
    # ------------------------------------------------------------------
    def _pending_swap_values(self, match):
        return [
            {
                "swap_id": swap_id,
                "from_player": request["from_player"],
                "to_player": request["to_player"],
                "status": request["status"],
            }
            for swap_id, request in match.pending_swaps.items()
        ]

    def _players_share_team(self, match, first, second):
        return ((first in match.blue_team and second in match.blue_team) or
                (first in match.red_team and second in match.red_team))

    def request_swap(self, match_id, from_player, to_player):
        with self._lock:
            match = self._require_match(match_id)
            self._refresh_match(match)
            self._require_active(match, "pick")
            self._require_player(match, from_player)
            self._require_player(match, to_player)
            if from_player == to_player or \
                    not self._players_share_team(
                        match, from_player, to_player
                    ):
                return {
                    "match_id": match_id,
                    "swap_id": None,
                    "status": SWAP_CONFLICT,
                }
            if match.picks.get(from_player) is None or \
                    match.picks.get(to_player) is None:
                return {
                    "match_id": match_id,
                    "swap_id": None,
                    "status": SWAP_CONFLICT,
                }

            for existing in match.pending_swaps.values():
                if {existing["from_player"], existing["to_player"]} & {
                        from_player, to_player}:
                    return {
                        "match_id": match_id,
                        "swap_id": None,
                        "status": SWAP_CONFLICT,
                    }

            self._swap_sequence += 1
            swap_id = "swap-%d" % self._swap_sequence
            match.pending_swaps[swap_id] = {
                "from_player": from_player,
                "to_player": to_player,
                "status": "pending",
            }
            return {
                "match_id": match_id,
                "swap_id": swap_id,
                "from_player": from_player,
                "to_player": to_player,
                "status": "pending",
            }

    def accept_swap(self, match_id, swap_id):
        with self._lock:
            match = self._require_match(match_id)
            self._refresh_match(match)
            if match.started or match.phase != "pick" or match.paused:
                raise DraftError(SWAP_CONFLICT)
            request = match.pending_swaps.get(swap_id)
            if request is None:
                raise DraftError(SWAP_CONFLICT)

            from_player = request["from_player"]
            to_player = request["to_player"]
            from_champion = match.picks.get(from_player)
            to_champion = match.picks.get(to_player)
            if from_champion is None or to_champion is None:
                match.pending_swaps.pop(swap_id, None)
                raise DraftError(SWAP_CONFLICT)

            match.picks[from_player], match.picks[to_player] = (
                to_champion, from_champion
            )
            match.pending_swaps.pop(swap_id, None)
            for candidate_id, candidate in list(match.pending_swaps.items()):
                if {candidate["from_player"], candidate["to_player"]} & {
                        from_player, to_player}:
                    match.pending_swaps.pop(candidate_id, None)
            return {
                "match_id": match_id,
                "swap_id": swap_id,
                "status": "swapped",
                "lineup": dict(match.picks),
            }

    # ------------------------------------------------------------------
    # Game start and persistence
    # ------------------------------------------------------------------
    def _validate_complete_lineup(self, match):
        lineup = match.picks
        if any(lineup.get(player_id) is None
               for player_id in match.players):
            raise DraftError(DRAFT_INCOMPLETE)
        chosen = [lineup[player_id] for player_id in match.players]
        if len(chosen) != len(set(chosen)):
            raise DraftError(DRAFT_INCOMPLETE)
        banned = {entry["champion"] for entry in match.bans}
        if any(champion not in match.champion_pool or champion in banned
               for champion in chosen):
            raise DraftError(DRAFT_INCOMPLETE)
        return {player_id: lineup[player_id] for player_id in match.players}

    def start_game(self, match_id, persist_hook=None):
        with self._lock:
            match = self._require_match(match_id)
            self._refresh_match(match)
            if match.started:
                raise DraftError(GAME_STARTED)
            if match.released:
                raise DraftError(WRONG_PHASE)
            if match.phase not in ("pick", "complete"):
                raise DraftError(DRAFT_INCOMPLETE)

            lineup = self._validate_complete_lineup(match)
            if persist_hook is not None:
                try:
                    persisted = persist_hook(dict(lineup))
                    if persisted is False:
                        raise RuntimeError("persistence hook rejected draft")
                except Exception as exc:
                    # Keep every lock in place: a retry must persist the same
                    # draft, not re-run champion selection.
                    match.phase = "pick"
                    match.deadline = self.server_time + self.pick_seconds
                    match.paused = False
                    match.paused_remaining = None
                    self._clear_pending_swaps(match)
                    raise DraftError(PERSIST_FAILED) from exc

            match.started = True
            match.phase = "started"
            match.deadline = self.server_time
            match.open_seats = 0
            self._clear_pending_swaps(match)
            return {
                "match_id": match_id,
                "status": "started",
                "lineup": dict(lineup),
                "bans": [entry["champion"] for entry in match.bans],
            }

    # ------------------------------------------------------------------
    # Observer and persistence snapshots
    # ------------------------------------------------------------------
    def spectators_view(self, match_id):
        with self._lock:
            state = self.phase_state(match_id)
            return {
                "match_id": match_id,
                "phase": state["phase"],
                "remaining": state["remaining"],
                "bans": state["bans"],
                "picks": state["picks"],
                "actions": state["actions"],
                "pending_swaps": state["pending_swaps"],
            }

    def snapshot(self):
        with self._lock:
            for match in self._matches.values():
                self._refresh_match(match)
            matches = {}
            for match_id, match in self._matches.items():
                matches[match_id] = {
                    "match_id": match.match_id,
                    "blue_team": list(match.blue_team),
                    "red_team": list(match.red_team),
                    "champion_pool": list(match.champion_pool),
                    "phase": match.phase,
                    "deadline": match.deadline,
                    "paused": match.paused,
                    "paused_remaining": match.paused_remaining,
                    "bans": copy.deepcopy(match.bans),
                    "picks": dict(match.picks),
                    "actions": copy.deepcopy(match.actions),
                    "pending_swaps": copy.deepcopy(match.pending_swaps),
                    "open_seats": match.open_seats,
                    "released": match.released,
                    "started": match.started,
                    "next_action_sequence": match.next_action_sequence,
                }
            return {
                "version": 1,
                "server_time": self.server_time,
                "swap_sequence": self._swap_sequence,
                "matches": matches,
            }

    def restore(self, snapshot):
        if not isinstance(snapshot, dict):
            raise DraftError(INVALID_SNAPSHOT)
        matches_snapshot = snapshot.get("matches")
        if not isinstance(matches_snapshot, dict):
            raise DraftError(INVALID_SNAPSHOT)

        with self._lock:
            # The server clock is monotonic.  A stale client snapshot may not
            # move it backwards and reopen an expired phase.
            snapshot_time = float(snapshot.get("server_time", self.server_time))
            self.server_time = max(self.server_time, snapshot_time)

            restored = {}
            for match_id, data in matches_snapshot.items():
                try:
                    match = _Match(
                        match_id=str(data.get("match_id", match_id)),
                        blue_team=list(data["blue_team"]),
                        red_team=list(data["red_team"]),
                        champion_pool=list(data["champion_pool"]),
                        phase=data.get("phase", "ban"),
                        deadline=float(data.get("deadline", self.server_time)),
                        paused=bool(data.get("paused", False)),
                        paused_remaining=data.get("paused_remaining"),
                        bans=copy.deepcopy(data.get("bans", [])),
                        picks=dict(data.get("picks", {})),
                        actions=copy.deepcopy(data.get("actions", [])),
                        pending_swaps=copy.deepcopy(
                            data.get("pending_swaps", {})
                        ),
                        open_seats=int(data.get("open_seats", 0)),
                        released=bool(data.get("released", False)),
                        started=bool(data.get("started", False)),
                        next_action_sequence=int(
                            data.get("next_action_sequence", 1)
                        ),
                    )
                except (KeyError, TypeError, ValueError) as exc:
                    raise DraftError(INVALID_SNAPSHOT) from exc

                previous = self._matches.get(match.match_id)
                if previous is not None and \
                        _PHASE_ORDER.get(previous.phase, -1) > \
                        _PHASE_ORDER.get(match.phase, -1):
                    # Never roll a live match back into an earlier phase.
                    match = previous
                elif previous is not None and previous.released and \
                        not match.released:
                    # Released seats cannot be reopened by a stale snapshot.
                    match = previous
                restored[match.match_id] = match

            self._matches = restored
            self._swap_sequence = int(
                snapshot.get("swap_sequence", self._swap_sequence)
            )
            for match in self._matches.values():
                self._refresh_match(match)
            return self.snapshot()
