import pytest

import wave_spawn_timeline as wt

WAVES = [
    {"wave_id": 1, "count": 3, "interval_ms": 100, "lanes": ["A"]},
    {"wave_id": 2, "count": 2, "interval_ms": 100, "lanes": ["A"]},
]


def make(budget=8, waves=None):
    timeline = wt.WaveTimeline(season_id="s1", frame_budget=budget)
    timeline.load_waves(WAVES if waves is None else waves)
    return timeline


def test_pause_freezes_clock_and_prevents_catch_up_spawns():
    timeline = make()
    timeline.update(0)
    before = timeline.stats()["spawned"]
    assert before == 1
    frozen = timeline.now_ms()
    timeline.set_paused(True)
    timeline.update(5000)
    assert timeline.now_ms() == frozen
    assert timeline.stats()["spawned"] == before
    timeline.set_paused(False)
    timeline.update(100)
    assert timeline.stats()["spawned"] == before + 1


def test_speed_changes_logical_step_not_real_time():
    timeline = make()
    timeline.set_speed(2.0)
    timeline.update(50)
    assert timeline.stats()["spawned"] == 2
    assert timeline.now_ms() == 100
    timeline.set_speed(1.0)
    timeline.update(50)
    assert timeline.stats()["spawned"] == 2
    timeline.update(50)
    assert timeline.stats()["spawned"] == 3


def test_duplicate_wave_trigger_is_ignored():
    timeline = make()
    assert timeline.trigger_wave(1) is True
    assert timeline.trigger_wave(1) is False
    timeline.update(0)
    assert timeline.stats()["spawned"] == 1


def test_next_wave_waits_until_previous_wave_is_cleared():
    waves = [
        {"wave_id": 1, "count": 1, "interval_ms": 0, "lanes": ["A"]},
        {"wave_id": 2, "count": 1, "interval_ms": 0, "lanes": ["A"]},
    ]
    timeline = make(waves=waves)
    spawned = timeline.update(1000)
    stats = timeline.stats()
    assert stats["wave"] == 1
    assert stats["spawned"] == 1
    assert len(spawned) == 1
    timeline.kill(spawned[0]["monster_id"])
    timeline.update(0)
    assert timeline.stats()["wave"] == 2
    assert timeline.stats()["spawned"] == 2


def test_checkpoint_restores_stats_rewards_and_clock():
    waves = [{"wave_id": 1, "count": 1, "interval_ms": 0, "lanes": ["A"]}]
    timeline = make(waves=waves)
    spawned = timeline.update(0)
    timeline.kill(spawned[0]["monster_id"])
    timeline.claim_reward(1)
    saved = timeline.snapshot()
    restored = make(waves=waves)
    restored.restore(saved)
    assert restored.stats()["killed"] == 1
    assert restored.claim_reward(1) is False
    assert restored.now_ms() == timeline.now_ms()


def test_leak_only_counts_monsters_that_are_still_alive():
    waves = [{"wave_id": 1, "count": 1, "interval_ms": 0, "lanes": ["A"]}]
    timeline = make(waves=waves)
    spawned = timeline.update(0)
    monster = spawned[0]["monster_id"]
    timeline.kill(monster)
    assert timeline.leak(monster) is False
    assert timeline.stats()["leaked"] == 0


def test_spawns_rotate_across_all_lanes():
    waves = [{"wave_id": 1, "count": 6, "interval_ms": 0, "lanes": ["A", "B", "C"]}]
    timeline = make(budget=6, waves=waves)
    spawned = timeline.update(0)
    assert [entry["lane"] for entry in spawned] == ["A", "B", "C", "A", "B", "C"]


def test_wave_reward_claimed_once_even_after_restore():
    waves = [{"wave_id": 1, "count": 1, "interval_ms": 0, "lanes": ["A"]}]
    timeline = make(waves=waves)
    spawned = timeline.update(0)
    timeline.kill(spawned[0]["monster_id"])
    assert timeline.claim_reward(1) is True
    assert timeline.claim_reward(1) is False
    saved = timeline.snapshot()
    restored = make(waves=waves)
    restored.restore(saved)
    assert restored.claim_reward(1) is False


def test_kill_is_idempotent_for_the_same_monster():
    waves = [{"wave_id": 1, "count": 2, "interval_ms": 0, "lanes": ["A"]}]
    timeline = make(waves=waves)
    spawned = timeline.update(0)
    monster = spawned[0]["monster_id"]
    assert timeline.kill(monster) is True
    assert timeline.kill(monster) is False
    assert timeline.stats()["killed"] == 1


def test_frame_budget_limits_spawns_per_update():
    waves = [{"wave_id": 1, "count": 10, "interval_ms": 0, "lanes": ["A"]}]
    timeline = make(budget=3, waves=waves)
    assert len(timeline.update(0)) == 3
    assert len(timeline.update(0)) == 3
    assert len(timeline.update(0)) == 3
    assert len(timeline.update(0)) == 1


def test_counts_obey_the_conservation_relation():
    waves = [{"wave_id": 1, "count": 5, "interval_ms": 0, "lanes": ["A"]}]
    timeline = make(budget=5, waves=waves)
    spawned = timeline.update(0)
    timeline.kill(spawned[0]["monster_id"])
    timeline.kill(spawned[1]["monster_id"])
    timeline.leak(spawned[2]["monster_id"])
    stats = timeline.stats()
    assert stats["spawned"] == 5
    assert stats["alive"] == 2
    assert stats["killed"] == 2
    assert stats["leaked"] == 1
    assert stats["spawned"] == stats["alive"] + stats["killed"] + stats["leaked"]


def test_invalid_wave_config_raises():
    timeline = wt.WaveTimeline(season_id="s1")
    with pytest.raises(wt.WaveConfigError):
        timeline.load_waves([{"wave_id": 1, "count": -1, "interval_ms": 0,
                              "lanes": ["A"]}])
    with pytest.raises(wt.WaveConfigError):
        timeline.load_waves([{"wave_id": 1, "count": 1, "interval_ms": -5,
                              "lanes": ["A"]}])


def test_client_predicted_kill_does_not_advance_the_wave():
    waves = [
        {"wave_id": 1, "count": 1, "interval_ms": 0, "lanes": ["A"]},
        {"wave_id": 2, "count": 1, "interval_ms": 0, "lanes": ["A"]},
    ]
    timeline = make(waves=waves)
    spawned = timeline.update(0)
    assert len(spawned) == 1
    assert timeline.kill("predicted-not-spawned") is False
    timeline.update(1000)
    assert timeline.stats()["wave"] == 1
    assert timeline.stats()["spawned"] == 1


def test_boss_wave_starts_only_after_normal_wave_is_cleared():
    waves = [
        {"wave_id": 1, "count": 2, "interval_ms": 0, "lanes": ["A"],
         "boss": False},
        {"wave_id": 2, "count": 1, "interval_ms": 0, "lanes": ["A"],
         "boss": True},
    ]
    timeline = make(budget=8, waves=waves)
    first_batch = timeline.update(0)
    assert {entry["wave_id"] for entry in first_batch} == {1}
    for entry in first_batch:
        timeline.kill(entry["monster_id"])
    assert timeline.stats()["cleared"] is True
    second_batch = timeline.update(0)
    assert {entry["wave_id"] for entry in second_batch} == {2}


def test_snapshot_keeps_season_and_resumes_current_wave():
    waves = [
        {"wave_id": 1, "count": 1, "interval_ms": 0, "lanes": ["A"]},
        {"wave_id": 2, "count": 1, "interval_ms": 50, "lanes": ["A"]},
    ]
    timeline = make(waves=waves)
    spawned = timeline.update(0)
    timeline.kill(spawned[0]["monster_id"])
    timeline.update(25)
    saved = timeline.snapshot()
    restored = make(waves=waves)
    restored.restore(saved)
    assert restored.season_id == "s1"
    assert restored.stats()["wave"] == 2
    restored.update(25)
    assert restored.stats()["spawned"] == 2
