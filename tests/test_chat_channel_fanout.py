import pytest

import chat_channel_fanout as chat


def make(**kwargs):
    hub = chat.ChannelFanout(**kwargs)
    hub.create_channel("world")
    hub.create_channel("team")
    hub.connect("c1", "a1", server="s1")
    hub.connect("c2", "a2", server="s1")
    hub.subscribe("c1", "world")
    hub.subscribe("c2", "world")
    return hub


def test_channel_sequence_is_monotonic():
    hub = make()
    hub.send("req1", "c1", "world", "one")
    hub.send("req2", "c1", "world", "two")
    hub.send("req3", "c1", "world", "three")
    assert [entry["seq"] for entry in hub.deliver("c1")] == [1, 2, 3]


def test_cross_server_messages_are_delivered_in_sequence_order():
    hub = make()
    hub.ingest("remote", 3, "world", "three", "a3")
    hub.ingest("remote", 1, "world", "one", "a3")
    hub.ingest("remote", 2, "world", "two", "a3")
    assert [entry["text"] for entry in hub.deliver("c1")] == [
        "one", "two", "three"]


def test_duplicate_gateway_request_is_stored_once():
    hub = make()
    hub.send("req1", "c1", "world", "hello")
    hub.send("req1", "c1", "world", "hello")
    assert len(hub.deliver("c1")) == 1


def test_mute_applies_on_every_server():
    hub = make()
    hub.connect("c9", "a1", server="s2")
    hub.mute("a1", until_ms=10 ** 12)
    with pytest.raises(chat.ChatError) as first:
        hub.send("req1", "c1", "world", "hello")
    assert first.value.code == "MUTED"
    with pytest.raises(chat.ChatError) as second:
        hub.send("req2", "c9", "world", "hello")
    assert second.value.code == "MUTED"


def test_blocked_sender_is_hidden_from_receiver_only():
    hub = make()
    hub.block("a2", "a1")
    hub.send("req1", "c1", "world", "hello")
    assert hub.deliver("c2") == []
    assert len(hub.deliver("c1")) == 1


def test_channel_level_permission_is_enforced_server_side():
    hub = make()
    hub.create_channel("officers", min_level=50)
    hub.subscribe("c1", "officers")
    with pytest.raises(chat.ChatError) as info:
        hub.send("req1", "c1", "officers", "hello", level=10)
    assert info.value.code == "FORBIDDEN"


def test_unknown_channel_reports_no_channel_code():
    hub = make()
    with pytest.raises(chat.ChatError) as info:
        hub.send("req1", "c1", "missing", "hello")
    assert info.value.code == "NO_CHANNEL"


def test_overlong_message_is_rejected_before_storage():
    hub = make(max_length=8)
    with pytest.raises(chat.ChatError) as info:
        hub.send("req1", "c1", "world", "0123456789")
    assert info.value.code == "TOO_LONG"
    assert hub.deliver("c1") == []


def test_restart_restores_recent_history():
    hub = make()
    hub.send("req1", "c1", "world", "one")
    hub.send("req2", "c1", "world", "two")
    saved = hub.snapshot()
    restarted = chat.ChannelFanout()
    restarted.restore(saved)
    restarted.connect("c1", "a1")
    restarted.subscribe("c1", "world")
    assert [entry["text"] for entry in restarted.backfill("c1", "world", 0)] == [
        "one", "two"]


def test_rename_is_reflected_for_old_messages():
    hub = make()
    hub.send("req1", "c2", "world", "hello")
    hub.rename("a2", "NewName")
    assert hub.name_of("a2") == "NewName"
    message = hub.deliver("c1")[0]
    assert message["author_id"] == "a2"


def test_filter_runs_before_message_is_split_into_packets():
    hub = make(max_length=8)
    hub.set_filter(["badword"])
    hub.send("req1", "c1", "world", "aa badword bb")
    delivered = hub.deliver("c1")
    assert delivered
    assert all("badword" not in entry["text"] for entry in delivered)


def test_switching_channel_drops_the_old_subscription():
    hub = make()
    hub.switch("c1", "team")
    hub.send("req1", "c2", "world", "hello")
    assert hub.deliver("c1") == []
    hub.send("req2", "c2", "team", "team message")
    assert len(hub.deliver("c1")) == 1


def test_backfill_and_live_push_do_not_duplicate_a_sequence():
    hub = make()
    hub.send("req1", "c1", "world", "one")
    hub.send("req2", "c1", "world", "two")
    backfilled = hub.backfill("c1", "world", 0)
    assert len(backfilled) == 2
    hub.ingest("remote", 2, "world", "two", "a1")
    assert hub.deliver("c1") == []


def test_duplicate_cross_server_segment_is_stored_once():
    hub = make()
    hub.ingest("remote", 1, "world", "one", "a3")
    hub.ingest("remote", 1, "world", "one", "a3")
    assert len(hub.backfill("c1", "world", 0)) == 1


def test_block_follows_the_account_across_servers():
    hub = make()
    hub.connect("c3", "a2", server="s2")
    hub.subscribe("c3", "world")
    hub.block("a2", "a1")
    hub.send("req1", "c1", "world", "hello")
    assert hub.deliver("c3") == []
