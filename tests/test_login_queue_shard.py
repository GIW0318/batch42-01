import pytest

import login_queue_shard
from login_queue_shard import LoginQueueShard, QueueError


def test_duplicate_enqueue_returns_already_queued():
    queue = LoginQueueShard()
    first = queue.enqueue("acc-1")
    assert first["status"] == "queued"
    assert first["position"] == 1
    second = queue.enqueue("acc-1")
    assert second["status"] == "already_queued"
    assert queue.queue_length() == 1


def test_token_is_only_issued_to_queue_head():
    queue = LoginQueueShard()
    queue.enqueue("acc-1")
    queue.enqueue("acc-2")
    with pytest.raises(QueueError) as exc:
        queue.issue_token("acc-2")
    assert exc.value.code == "not_at_front"
    issued = queue.issue_token("acc-1")
    assert issued["status"] == "issued"
    assert isinstance(issued["token"], str) and issued["token"]


def test_token_is_single_use_and_replay_is_logged():
    queue = LoginQueueShard()
    queue.enqueue("acc-1")
    token = queue.issue_token("acc-1")["token"]
    first = queue.consume_token(token)
    assert first["status"] == "entered"
    with pytest.raises(QueueError) as exc:
        queue.consume_token(token)
    assert exc.value.code == "token_replayed"
    assert len(queue.replay_log()) == 1


def test_expired_token_returns_account_to_original_position():
    queue = LoginQueueShard(token_ttl=30)
    queue.enqueue("acc-1")
    queue.enqueue("acc-2")
    original = queue.position("acc-1")
    token = queue.issue_token("acc-1")["token"]
    queue.advance(31)
    with pytest.raises(QueueError) as exc:
        queue.consume_token(token)
    assert exc.value.code == "token_expired"
    assert queue.position("acc-1") == original


def test_vip_insert_does_not_move_normal_positions():
    queue = LoginQueueShard()
    queue.enqueue("acc-1")
    queue.enqueue("acc-2")
    before = (queue.position("acc-1"), queue.position("acc-2"))
    queue.enqueue("vip-1", vip=True)
    assert (queue.position("acc-1"), queue.position("acc-2")) == before


def test_member_upgrade_keeps_single_entry_and_improves_position():
    queue = LoginQueueShard()
    queue.enqueue("acc-1")
    queue.enqueue("acc-2")
    queue.upgrade_vip("acc-2")
    assert queue.queue_length() == 2
    assert queue.position("acc-2") < queue.position("acc-1")
    assert queue.enqueue("acc-2")["status"] == "already_queued"


def test_restart_continues_position_sequence_from_cursor():
    queue = LoginQueueShard()
    for index in range(3):
        queue.enqueue("acc-%d" % index)
    snapshot = queue.snapshot()

    restored = LoginQueueShard()
    restored.restore(snapshot)
    fourth = restored.enqueue("acc-4")
    assert fourth["position"] == 4


def test_resize_does_not_invalidate_issued_token():
    queue = LoginQueueShard(capacity=5)
    queue.enqueue("acc-1")
    token = queue.issue_token("acc-1")["token"]
    queue.resize("s1", 10)
    assert queue.stats("s1")["capacity"] == 10
    assert queue.consume_token(token)["status"] == "entered"


def test_kick_keeps_position_within_hold_window():
    queue = LoginQueueShard(position_hold=60)
    queue.enqueue("acc-1")
    queue.enqueue("acc-2")
    queue.enqueue("acc-3")
    held = queue.position("acc-2")
    queue.kick("acc-2")
    assert queue.queue_length() == 2
    again = queue.enqueue("acc-2")
    assert again["position"] == held
    assert queue.queue_length() == 3


def test_kick_after_hold_timeout_releases_position():
    queue = LoginQueueShard(position_hold=60)
    queue.enqueue("acc-1")
    queue.enqueue("acc-2")
    queue.enqueue("acc-3")
    queue.kick("acc-2")
    queue.advance(61)
    again = queue.enqueue("acc-2")
    assert again["position"] == 3


def test_heartbeat_does_not_double_count_online_players():
    queue = LoginQueueShard()
    queue.enqueue("acc-1")
    token = queue.issue_token("acc-1")["token"]
    queue.consume_token(token)
    queue.heartbeat("acc-1")
    queue.heartbeat("acc-1")
    assert queue.online_count() == 1


def test_same_account_cannot_queue_in_two_shards():
    queue = LoginQueueShard()
    queue.enqueue("acc-1", "s1")
    with pytest.raises(QueueError) as exc:
        queue.enqueue("acc-1", "s2")
    assert exc.value.code == "already_queued"


def test_day_boundary_does_not_clear_queue():
    queue = LoginQueueShard()
    queue.enqueue("acc-1")
    queue.enqueue("acc-2")
    queue.set_day(2)
    assert queue.queue_length() == 2
    assert queue.position("acc-1") == 1


def test_stats_separate_queued_and_entered():
    queue = LoginQueueShard(capacity=7)
    queue.enqueue("acc-1")
    queue.enqueue("acc-2")
    token = queue.issue_token("acc-1")["token"]
    queue.consume_token(token)
    stats = queue.stats("s1")
    assert stats["queued"] == 1
    assert stats["entered"] == 1
    assert stats["capacity"] == 7


def test_estimated_wait_uses_server_elapsed_time():
    queue = LoginQueueShard()
    queue.enqueue("acc-1")
    queue.enqueue("acc-2")
    queue.enqueue("acc-3")
    before = queue.estimated_wait("acc-3")
    assert before > 0
    queue.advance(120)
    after = queue.estimated_wait("acc-3")
    assert after >= 0
    assert after <= before
