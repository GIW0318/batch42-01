import pytest

import ranked_draft_pick
from ranked_draft_pick import DraftError, RankedDraftPick


POOL = ["hero-a", "hero-b", "hero-c", "hero-d", "hero-e", "hero-f",
        "hero-g", "hero-h", "hero-i", "hero-j"]


def _game():
    game = RankedDraftPick(champion_pool=POOL)
    game.start_draft("match-1",
                     blue_team=["b1", "b2", "b3", "b4", "b5"],
                     red_team=["r1", "r2", "r3", "r4", "r5"])
    return game


def test_duplicate_champion_pick_is_rejected():
    game = _game()
    game.advance_phase("match-1")
    game.pick("match-1", "b1", "hero-a")
    with pytest.raises(DraftError) as exc:
        game.pick("match-1", "r1", "hero-a")
    assert exc.value.code == "champion_taken"
    assert game.lineup("match-1")["r1"] is None


def test_ban_then_pick_same_champion_is_rejected():
    game = _game()
    game.ban("match-1", "b1", "hero-b")
    game.advance_phase("match-1")
    with pytest.raises(DraftError) as exc:
        game.pick("match-1", "r1", "hero-b")
    assert exc.value.code == "champion_taken"


def test_phase_advances_on_server_clock_only():
    game = _game()
    assert game.phase_state("match-1")["phase"] == "ban"
    assert game.phase_state("match-1")["remaining"] == 30
    game.advance(10)
    assert game.phase_state("match-1")["remaining"] == 20
    game.advance(20)
    assert game.phase_state("match-1")["phase"] == "pick"
    assert game.phase_state("match-1")["remaining"] == 30
    game.advance(30)
    assert game.phase_state("match-1")["phase"] == "complete"


def test_dodge_releases_all_seats_exactly_once():
    game = _game()
    assert game.open_seats("match-1") == 10
    first = game.dodge("match-1", "b1")
    assert first["status"] == "dodged"
    assert game.open_seats("match-1") == 0
    second = game.dodge("match-1", "b2")
    assert second["status"] == "already_released"
    assert game.open_seats("match-1") == 0


def test_reconnect_restores_phase_remaining_and_bans():
    game = _game()
    game.ban("match-1", "b1", "hero-a")
    game.advance(10)
    state = game.reconnect("match-1", "b1")
    assert state["phase"] == "ban"
    assert state["remaining"] == 20
    assert state["bans"] == ["hero-a"]
    game.advance(20)
    state = game.reconnect("match-1", "b1")
    assert state["phase"] == "pick"
    assert state["bans"] == ["hero-a"]


def test_pending_swap_is_cleared_at_phase_end():
    game = _game()
    game.advance_phase("match-1")
    game.pick("match-1", "b1", "hero-a")
    game.pick("match-1", "b2", "hero-c")
    swap = game.request_swap("match-1", "b1", "b2")
    assert swap["status"] == "pending"
    game.advance_phase("match-1")
    assert game.phase_state("match-1")["pending_swaps"] == []
    assert game.lineup("match-1")["b1"] == "hero-a"
    assert game.lineup("match-1")["b2"] == "hero-c"


def test_swap_changes_lineup_only_after_accept():
    game = _game()
    game.advance_phase("match-1")
    game.pick("match-1", "b1", "hero-a")
    game.pick("match-1", "b2", "hero-c")
    swap = game.request_swap("match-1", "b1", "b2")
    assert game.lineup("match-1")["b1"] == "hero-a"
    accepted = game.accept_swap("match-1", swap["swap_id"])
    assert accepted["status"] == "swapped"
    assert game.lineup("match-1")["b1"] == "hero-c"
    assert game.lineup("match-1")["b2"] == "hero-a"


def test_simultaneous_mutual_swaps_apply_once():
    game = _game()
    game.advance_phase("match-1")
    game.pick("match-1", "b1", "hero-a")
    game.pick("match-1", "b2", "hero-c")
    first = game.request_swap("match-1", "b1", "b2")
    second = game.request_swap("match-1", "b2", "b1")
    assert second["status"] == "swap_conflict"
    game.accept_swap("match-1", first["swap_id"])
    lineup = game.lineup("match-1")
    assert sorted([champion for champion in lineup.values()
                   if champion is not None]) == ["hero-a", "hero-c"]


def test_timeout_auto_pick_skips_banned_and_locked_picks():
    game = _game()
    game.ban("match-1", "b1", "hero-a")
    game.advance_phase("match-1")
    game.pick("match-1", "b1", "hero-c")
    game.advance(30)
    lineup = game.lineup("match-1")
    assert lineup["b1"] == "hero-c"
    auto_picked = [champion for champion in lineup.values()
                   if champion is not None]
    assert "hero-a" not in auto_picked
    assert len(auto_picked) == len(set(auto_picked))


def test_start_game_requires_complete_draft():
    game = _game()
    with pytest.raises(DraftError) as exc:
        game.start_game("match-1")
    assert exc.value.code == "draft_incomplete"


def test_spectator_view_matches_server_actions():
    game = _game()
    game.ban("match-1", "b1", "hero-a")
    game.advance_phase("match-1")
    game.pick("match-1", "b1", "hero-c")
    view = game.spectators_view("match-1")
    assert view["actions"] == game.phase_state("match-1")["actions"]
    assert view["phase"] == "pick"


def test_same_player_cannot_lock_two_champions():
    game = _game()
    game.advance_phase("match-1")
    game.pick("match-1", "b1", "hero-a")
    with pytest.raises(DraftError) as exc:
        game.pick("match-1", "b1", "hero-b")
    assert exc.value.code == "already_picked"
    assert game.lineup("match-1")["b1"] == "hero-a"


def test_unknown_champion_does_not_change_draft():
    game = _game()
    game.advance_phase("match-1")
    with pytest.raises(DraftError) as exc:
        game.pick("match-1", "b1", "hero-from-next-patch")
    assert exc.value.code == "unknown_champion"
    assert game.lineup("match-1")["b1"] is None


def test_dodge_after_game_start_is_rejected():
    game = _game()
    game.advance_phase("match-1")
    for player, champion in [("b1", "hero-a"), ("r1", "hero-b"),
                             ("b2", "hero-c"), ("r2", "hero-d"),
                             ("b3", "hero-e"), ("r3", "hero-f"),
                             ("b4", "hero-g"), ("r4", "hero-h"),
                             ("b5", "hero-i"), ("r5", "hero-j")]:
        game.pick("match-1", player, champion)
    game.start_game("match-1")
    with pytest.raises(DraftError) as exc:
        game.dodge("match-1", "b4")
    assert exc.value.code == "game_started"


def test_persist_failure_returns_to_pick_phase():
    game = _game()
    game.advance_phase("match-1")
    for player, champion in [("b1", "hero-a"), ("r1", "hero-b"),
                             ("b2", "hero-c"), ("r2", "hero-d"),
                             ("b3", "hero-e"), ("r3", "hero-f"),
                             ("b4", "hero-g"), ("r4", "hero-h"),
                             ("b5", "hero-i"), ("r5", "hero-j")]:
        game.pick("match-1", player, champion)

    def boom(lineup):
        raise RuntimeError("database unavailable")

    with pytest.raises(DraftError) as exc:
        game.start_game("match-1", persist_hook=boom)
    assert exc.value.code == "persist_failed"
    assert game.phase_state("match-1")["phase"] == "pick"
    started = game.start_game("match-1")
    assert started["status"] == "started"
    assert started["lineup"]["b1"] == "hero-a"
