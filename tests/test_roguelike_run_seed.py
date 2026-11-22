import pytest

import roguelike_run_seed
from roguelike_run_seed import RoguelikeRunSeed, RunError


def test_same_seed_produces_same_random_sequence():
    game = RoguelikeRunSeed()
    game.start_run("run-1", "acc", seed=42)
    game.start_run("run-2", "acc", seed=42)
    seq_one = [game.next_random("run-1") for _ in range(5)]
    seq_two = [game.next_random("run-2") for _ in range(5)]
    assert seq_one == seq_two
    game.start_run("run-3", "acc", seed=43)
    assert [game.next_random("run-3") for _ in range(5)] != seq_one


def test_random_sequence_continues_after_restart_without_reroll():
    game = RoguelikeRunSeed()
    game.start_run("run-1", "acc", seed=42)
    first_three = [game.next_random("run-1") for _ in range(3)]
    snapshot = game.snapshot()
    expected = [game.next_random("run-1") for _ in range(3)]

    restored = RoguelikeRunSeed()
    restored.restore(snapshot)
    got = [restored.next_random("run-1") for _ in range(3)]
    assert got == expected
    assert got != first_three


def test_reopened_chest_returns_original_reward():
    game = RoguelikeRunSeed()
    game.start_run("run-1", "acc", seed=7)
    first = game.open_chest("run-1", 1, "chest-1")
    second = game.open_chest("run-1", 1, "chest-1")
    assert first["status"] == "looted"
    assert second["status"] == "already_looted"
    assert second["items"] == first["items"]


def test_chest_retry_does_not_duplicate_inventory():
    game = RoguelikeRunSeed()
    game.start_run("run-1", "acc", seed=7)
    first = game.open_chest("run-1", 1, "chest-1")
    count = len(game.inventory("run-1", "acc"))
    game.open_chest("run-1", 1, "chest-1")
    game.open_chest("run-1", 1, "chest-1")
    assert len(game.inventory("run-1", "acc")) == count


def test_recover_returns_to_last_committed_floor():
    game = RoguelikeRunSeed()
    game.start_run("run-1", "acc", seed=7)
    game.commit_floor("run-1", 2, {
        "floor": 2, "keys": 1, "relics": ["relic-a"], "hp": 80})
    game.pick_up("run-1", "temp-gem", "acc")
    snapshot = game.snapshot()

    restored = RoguelikeRunSeed()
    restored.restore(snapshot)
    state = restored.recover("run-1")
    assert state["floor"] == 2
    assert state["keys"] == 1
    assert state["relics"] == ["relic-a"]
    assert "temp-gem" not in restored.inventory("run-1", "acc")


def test_uncommitted_pickup_is_discarded_not_reissued():
    game = RoguelikeRunSeed()
    game.start_run("run-1", "acc", seed=7)
    game.commit_floor("run-1", 1, {
        "floor": 1, "keys": 0, "relics": [], "hp": 100})
    game.pick_up("run-1", "temp-gem", "acc")
    state = game.recover("run-1")
    assert state["floor"] == 1
    assert game.inventory("run-1", "acc") == []


def test_daily_seed_uses_server_day_not_client_day():
    game = RoguelikeRunSeed(server_day=5)
    first = game.daily_seed("acc")
    assert game.daily_seed("acc") == first
    assert game.daily_seed("acc", client_day=99) == first
    game.set_server_day(6)
    assert game.daily_seed("acc") != first


def test_daily_settlement_runs_once_per_account():
    game = RoguelikeRunSeed(server_day=5)
    game.start_run("daily-1", "acc", mode="daily")
    first = game.settle_daily("acc", "daily-1")
    second = game.settle_daily("acc", "daily-1")
    assert first["status"] == "settled"
    assert second["status"] == "already_settled"
    assert second["score"] == first["score"]
    assert len(game.leaderboard()) == 1


def test_run_started_before_day_boundary_settles_once():
    game = RoguelikeRunSeed(server_day=5)
    game.start_run("daily-1", "acc", mode="daily")
    game.set_server_day(6)
    first = game.settle_daily("acc", "daily-1")
    second = game.settle_daily("acc", "daily-1")
    assert first["status"] == "settled"
    assert second["status"] == "already_settled"


def test_mid_run_join_sees_relics_but_no_past_rewards():
    game = RoguelikeRunSeed()
    game.start_run("run-1", "acc", seed=9)
    game.commit_floor("run-1", 1, {
        "floor": 1, "keys": 2, "relics": ["relic-a"], "hp": 100})
    game.open_chest("run-1", 1, "chest-1")
    game.join_run("run-1", "friend")
    assert game.party_relics("run-1", "friend") == ["relic-a"]
    assert game.inventory("run-1", "friend") == []
    assert game.inventory("run-1", "acc") != []


def test_old_content_snapshot_is_rejected_on_recover():
    game = RoguelikeRunSeed(content_version="v1")
    game.start_run("run-1", "acc", seed=3)
    game.commit_floor("run-1", 1, {
        "floor": 1, "keys": 0, "relics": [], "hp": 100})
    snapshot = game.snapshot()

    newer = RoguelikeRunSeed(content_version="v2")
    newer.restore(snapshot)
    with pytest.raises(RunError) as exc:
        newer.recover("run-1")
    assert exc.value.code == "content_mismatch"


def test_revive_item_is_consumed_once():
    game = RoguelikeRunSeed()
    game.start_run("run-1", "acc", seed=3)
    game.grant_item("run-1", "revive-token", "one", count=1)
    game.grant_item("run-1", "revive-token", "two", count=0)
    first = game.use_revive("run-1", "revive-token", "one")
    assert first["status"] == "used"
    with pytest.raises(RunError) as exc:
        game.use_revive("run-1", "revive-token", "two")
    assert exc.value.code == "item_consumed"
    assert game.revive_count("run-1") == 1


def test_frame_ticks_do_not_consume_random_stream():
    ticked = RoguelikeRunSeed()
    ticked.start_run("run-1", "acc", seed=42)
    for _ in range(120):
        ticked.advance(0.016)
    ticked_value = ticked.next_random("run-1")

    fresh = RoguelikeRunSeed()
    fresh.start_run("run-2", "acc", seed=42)
    assert ticked_value == fresh.next_random("run-2")


def test_player_cannot_join_two_runs_at_once():
    game = RoguelikeRunSeed()
    game.start_run("run-1", "acc", seed=1)
    game.start_run("run-2", "acc-2", seed=2)
    game.join_run("run-1", "player-1")
    with pytest.raises(RunError) as exc:
        game.join_run("run-2", "player-1")
    assert exc.value.code == "already_in_run"
    reconnect = game.join_run("run-1", "player-1")
    assert reconnect["status"] == "already_joined"


def test_second_daily_run_does_not_create_second_leaderboard_entry():
    game = RoguelikeRunSeed(server_day=5)
    game.start_run("daily-1", "acc", mode="daily")
    game.start_run("daily-2", "acc", mode="daily")
    game.settle_daily("acc", "daily-1")
    second = game.settle_daily("acc", "daily-2")
    assert second["status"] == "already_settled"
    assert len(game.leaderboard()) == 1
    assert game.leaderboard()[0]["account_id"] == "acc"
