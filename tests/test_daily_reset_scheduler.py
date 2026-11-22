import pytest

import daily_reset_scheduler as dr

DAY = 86400000


def make():
    return dr.DailyReset()


def test_next_reset_follows_each_account_local_offset():
    reset = make()
    reset.set_account("east", 480)
    reset.set_account("west", -300)
    assert reset.next_reset_ms("east", 0) == 57600000
    assert reset.next_reset_ms("west", 0) == 18000000


def test_same_account_and_batch_runs_only_once():
    reset = make()
    reset.set_account("west", -300)
    first = reset.run(18000000)
    second = reset.run(18000000)
    assert first == ["west"]
    assert second == []


def test_offset_change_on_dst_day_does_not_reset_twice():
    reset = make()
    reset.set_account("west", -300)
    assert reset.run(18000000) == ["west"]
    reset.set_account("west", -240)
    assert reset.run(18000000) == []


def test_task_period_uses_completion_time_not_processing_time():
    reset = make()
    reset.set_account("west", -300)
    completed = 18000000 - 1000
    processed = 18000000 + 1000
    reset.submit_task("west", "quest1", completed, processed)
    assert reset.task_period("west", "quest1") == reset.batch_key("west", completed)
    assert reset.task_period("west", "quest1") != reset.batch_key("west", processed)


def test_weekly_account_is_not_cleared_by_daily_reset():
    reset = make()
    reset.set_account("daily", 0)
    reset.set_account("weekly", 0, period_days=7)
    reset.set_progress("daily", 3, ["quest1"])
    reset.set_progress("weekly", 4, ["quest2"])
    assert reset.run(DAY) == ["daily"]
    assert reset.state("weekly")["progress"] == 4
    assert reset.state("daily")["progress"] == 0


def test_online_reset_clears_progress_and_accepted_quests():
    reset = make()
    reset.set_account("west", -300)
    reset.set_progress("west", 5, ["quest1"])
    reset.run(18000000)
    state = reset.state("west")
    assert state["progress"] == 0
    assert state["accepted"] == []


def test_crash_mid_batch_resumes_the_remaining_accounts():
    reset = make()
    reset.set_account("a1", 0)
    reset.set_account("a2", 0)
    reset.set_account("a3", 0)
    assert reset.run(DAY, fail_after=1) == ["a1"]
    remaining = reset.run(DAY)
    assert remaining == ["a2", "a3"]
    assert reset.run(DAY) == []


def test_two_shards_do_not_execute_the_same_batch_twice():
    reset = make()
    reset.set_account("a1", 0)
    assert reset.run(DAY, shard="shard-a") == ["a1"]
    assert reset.run(DAY, shard="shard-b") == []


def test_maintenance_delay_postpones_the_reset():
    reset = make()
    reset.set_account("a1", 0)
    reset.set_maintenance_until(DAY + 7200000)
    assert reset.run(DAY + 1800000) == []
    assert reset.run(DAY + 7200001) == ["a1"]


def test_cross_server_character_resets_once_by_global_id():
    reset = make()
    reset.set_account("a1", 0, char_ids=["char1"])
    reset.set_account("a2", 0, char_ids=["char1"])
    reset.set_character_account("char1", "a1")
    reset.set_character_account("char1", "a2")
    assert reset.reset_character("char1") is True
    assert reset.reset_character("char1") is False


def test_compensation_is_deduplicated_per_reward():
    reset = make()
    reset.set_account("a1", 0)
    assert reset.compensate("a1", "1970-01-02", "reward-1") is True
    assert reset.compensate("a1", "1970-01-02", "reward-1") is False
    assert len(reset.compensations("a1")) == 1


def test_invalid_timezone_or_period_config_raises():
    reset = make()
    with pytest.raises(dr.ResetConfigError):
        reset.set_account("bad", 2000)
    with pytest.raises(dr.ResetConfigError):
        reset.set_account("bad", 0, period_days=0)


def test_leaderboard_is_archived_before_current_period_is_cleared():
    reset = make()
    reset.set_account("west", -300)
    reset.set_leaderboard("daily", {"p1": 100, "p2": 90})
    reset.run(18000000)
    assert reset.archive("daily") == {"p1": 100, "p2": 90}
    assert reset.leaderboard("daily") == {}


def test_batch_keys_are_unique_per_local_day_and_account():
    reset = make()
    reset.set_account("west", -300)
    assert reset.batch_key("west", 18000000) == reset.batch_key("west", 18000000 + 60000)
    assert reset.batch_key("west", 18000000) != reset.batch_key("west", 18000000 + DAY)


def test_run_account_leaves_other_accounts_untouched():
    reset = make()
    reset.set_account("a1", 0)
    reset.set_account("a2", 0)
    reset.set_progress("a1", 3)
    reset.set_progress("a2", 4)
    reset.run_account("a1", DAY)
    assert reset.state("a1")["progress"] == 0
    assert reset.state("a2")["progress"] == 4
