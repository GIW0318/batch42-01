import threading

import pytest

import territory_war_tick as twt


def new_war(**kwargs):
    return twt.TerritoryWarTick(**kwargs)


def test_contested_point_resets_progress_and_cannot_be_captured():
    war = new_war(capture_progress=2)
    war.add_point("p1", 1)
    war.enter("p1", "A")
    war.enter("p1", "B")
    war.tick(0.0)
    war.tick(5.0)
    result = war.tick(10.0)
    state = war.state("p1")
    assert state["owner"] is None
    assert state["progress"] == 0
    assert set(state["contest"]) == {"A", "B"}
    assert result["captured"] == []
    assert war.score("A") == 0
    assert war.score("B") == 0


def test_sole_faction_captures_and_owner_matches_scoring_faction():
    war = new_war(capture_progress=1, capture_award=10)
    war.add_point("p1", 1)
    war.enter("p1", "A")
    war.tick(0.0)
    result = war.tick(5.0)
    assert result["captured"] == [{"point": "p1", "faction": "A"}]
    assert war.state("p1")["owner"] == "A"
    assert war.score("A") == 10
    assert war.score("B") == 0
    war.tick(10.0)
    assert war.state("p1")["owner"] == "A"
    assert war.score("A") == 11
    assert war.score("B") == 0


def test_lanes_are_isolated_on_capture():
    war = new_war(capture_progress=1)
    war.add_point("p1", 1)
    war.add_point("p2", 2)
    war.set_reinforcements(2, "A", 5)
    war.enter("p1", "A")
    war.tick(0.0)
    war.tick(5.0)
    lane_one = war.lane_state(1)
    lane_two = war.lane_state(2)
    assert lane_one["points"]["p1"]["owner"] == "A"
    assert lane_two["points"]["p2"]["owner"] is None
    assert war.reinforcements(2, "A") == 5
    assert war.state("p2")["contest"] == []


def test_tick_catchup_is_bounded_and_reports_skipped_ticks():
    war = new_war(capture_progress=100, max_catchup_ticks=2)
    war.add_point("p1", 1)
    war.enter("p1", "A")
    war.tick(0.0)
    result = war.tick(15.0)
    assert result["skipped_ticks"] == 1
    assert war.state("p1")["progress"] == 2
    assert war.score("A") == 0


def test_truce_blocks_progress_capture_and_kill_score():
    war = new_war(capture_progress=100)
    war.add_point("p1", 1)
    war.join_match("c1", "A", account="acct1")
    war.enter("p1", "A")
    war.tick(0.0)
    war.tick(5.0)
    assert war.state("p1")["progress"] == 1
    war.set_truce(10.0, 20.0)
    war.tick(15.0)
    assert war.state("p1")["progress"] == 1
    denied = war.record_kill("c1", points=3, now=15.0)
    assert denied["accepted"] is False
    assert denied["reason"] == "truce"
    assert war.contribution("acct1") == 0
    war.tick(21.0)
    assert war.state("p1")["progress"] == 2


def test_zero_reinforcements_blocks_respawn():
    war = new_war()
    war.join_match("c1", "A", account="acct1")
    war.set_reinforcements(1, "A", 2)
    first = war.respawn(1, "A", "c1")
    second = war.respawn(1, "A", "c1")
    third = war.respawn(1, "A", "c1")
    assert first["allowed"] is True
    assert first["remaining"] == 1
    assert second["allowed"] is True
    assert second["remaining"] == 0
    assert third["allowed"] is False
    assert third["remaining"] == 0
    assert third["reason"] == "no reinforcements"
    assert war.reinforcements(1, "A") == 0


def test_quitting_freezes_contribution():
    war = new_war()
    war.join_match("c1", "A", account="acct1")
    scored = war.record_kill("c1", points=5, now=0.0)
    assert scored["accepted"] is True
    assert war.contribution("acct1") == 5
    war.leave_match("c1")
    denied = war.record_kill("c1", points=5, now=1.0)
    assert denied["accepted"] is False
    assert denied["reason"] == "not_in_match"
    assert war.contribution("acct1") == 5


def test_capture_report_is_idempotent():
    war = new_war(capture_award=10)
    war.add_point("p1", 1)
    war.enter("p1", "A")
    first = war.report_capture("p1", "A", "report-1")
    second = war.report_capture("p1", "A", "report-1")
    assert first["status"] == "captured"
    assert second["status"] == "duplicate"
    assert war.state("p1")["owner"] == "A"
    assert war.score("A") == 10


def test_settle_round_is_idempotent_and_mails_once():
    war = new_war()
    war.join_match("c1", "A", account="acct1")
    war.join_match("c2", "B", account="acct2")
    war.record_kill("c1", points=3, now=0.0)
    war.record_kill("c2", points=2, now=0.0)
    first = war.settle_round("round-1")
    second = war.settle_round("round-1")
    assert first["duplicate"] is False
    assert first["mails"] == 2
    assert second["duplicate"] is True
    assert second["mails"] == 2
    assert second["scores"] == first["scores"]


def test_reward_can_only_be_claimed_once():
    war = new_war()
    war.join_match("c1", "A", account="acct1")
    war.record_kill("c1", points=5, now=0.0)
    war.settle_round("round-1")
    first = war.claim_reward("round-1", "acct1")
    second = war.claim_reward("round-1", "acct1")
    assert first["status"] == "claimed"
    assert first["reward"] == 5
    assert second["status"] == "already_claimed"


def test_restart_restores_owner_progress_and_reinforcements():
    war = new_war(capture_progress=2)
    war.add_point("p1", 1)
    war.add_point("p2", 1)
    war.add_point("p3", 1)
    war.set_reinforcements(1, "A", 4)
    war.enter("p1", "A")
    war.enter("p2", "A")
    war.tick(0.0)
    war.tick(5.0)
    war.enter("p3", "A")
    war.tick(10.0)
    assert war.state("p1")["owner"] == "A"
    assert war.state("p2")["owner"] == "A"
    state = war.export_state()
    restarted = new_war(capture_progress=2)
    restarted.load_state(state)
    assert restarted.state("p1")["owner"] == "A"
    assert restarted.state("p2")["owner"] == "A"
    assert restarted.state("p3")["progress"] == 1
    assert restarted.reinforcements(1, "A") == 4
    restarted.tick(15.0)
    assert restarted.state("p3")["owner"] == "A"
    assert restarted.state("p3")["progress"] == 0


def test_out_of_bounds_entry_raises():
    war = new_war()
    war.add_point("p1", 1, boundary=(0, 0, 10, 10))
    with pytest.raises(twt.OutOfBoundsError):
        war.enter("p1", "A", position=(50, 50))
    assert war.state("p1")["contest"] == []


def test_same_account_two_characters_count_once():
    war = new_war()
    war.join_match("c1", "A", account="acct1")
    war.join_match("c2", "A", account="acct1")
    first = war.record_kill("c1", points=5, now=0.0)
    second = war.record_kill("c2", points=5, now=0.0)
    assert first["accepted"] is True
    assert second["accepted"] is False
    assert second["reason"] == "account_already_counted"
    assert war.contribution("acct1") == 5
    war.settle_round("round-1")
    reward = war.claim_reward("round-1", "acct1")
    assert reward["status"] == "claimed"
    assert reward["reward"] == 5


def test_switch_faction_is_locked_mid_round():
    war = new_war()
    war.join_match("c1", "A", account="acct1")
    with pytest.raises(twt.FactionLockedError):
        war.switch_faction("c1", "B")
    assert war.contribution("acct1") == 0


def test_concurrent_ticks_apply_single_tick():
    war = new_war(capture_progress=100)
    war.add_point("p1", 1)
    war.enter("p1", "A")
    war.tick(0.0)
    barrier = threading.Barrier(2)
    results = []
    results_lock = threading.Lock()

    def call():
        barrier.wait()
        result = war.tick(5.0)
        with results_lock:
            results.append(result)

    threads = [threading.Thread(target=call) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)

    assert len(results) == 2
    assert war.state("p1")["progress"] == 1
