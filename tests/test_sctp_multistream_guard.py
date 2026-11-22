import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from sctp_multistream_guard import SctpError, SctpStreamGuard
import sctp_multistream_guard


def test_streams_have_independent_ssn_sequences():
    guard = SctpStreamGuard(peer_streams=32)
    guard.register_association("a1", peer_streams=32)
    first = guard.send("a1", 0, b"x")
    other = guard.send("a1", 1, b"y")
    second = guard.send("a1", 0, b"z")
    assert [first["ssn"], other["ssn"], second["ssn"]] == [0, 0, 1]
    assert guard.next_ssn("a1", 0) == 2


def test_concurrent_sends_on_same_stream_use_unique_ssn():
    guard = SctpStreamGuard(peer_streams=32)
    guard.register_association("a1", peer_streams=32)
    barrier = threading.Barrier(16)

    def worker(index):
        barrier.wait()
        try:
            return guard.send("a1", 0, ("m%d" % index).encode())["ssn"]
        except Exception:
            return -1

    with ThreadPoolExecutor(max_workers=16) as pool:
        ssns = list(pool.map(worker, range(64)))
    assert sorted(ssns) == list(range(64))


def test_large_message_on_one_stream_does_not_block_other_stream():
    guard = SctpStreamGuard(peer_streams=32)
    guard.register_association("a1", peer_streams=32)
    partial = guard.receive_chunk("a1", 0, 1, 0, b"A" * 1000, last=False)
    assert partial["complete"] is False
    ready = guard.receive_chunk("a1", 1, 2, 0, b"small", last=True)
    assert ready["complete"] is True
    assert ready["payload"] == b"small"
    finished = guard.receive_chunk("a1", 0, 3, 0, b"B" * 10, last=True)
    assert finished["payload"] == b"A" * 1000 + b"B" * 10


def test_ssn_wrap_at_65536_is_not_treated_as_duplicate():
    guard = SctpStreamGuard()
    guard.register_association("a1")
    guard.receive_chunk("a1", 0, 1, 65535, b"old", last=True)
    result = guard.receive_chunk("a1", 0, 2, 0, b"new", last=True)
    assert result["duplicate"] is False
    assert result["payload"] == b"new"
    assert guard.duplicate_count("a1", 0) == 0


def test_abandoned_message_leaves_send_queue():
    guard = SctpStreamGuard()
    guard.register_association("a1")
    sent = guard.send("a1", 0, b"Q" * 20000)
    assert guard.pending("a1")["bytes"] >= 20000
    guard.abandon(sent["tsn"])
    assert guard.pending("a1")["bytes"] == 0
    assert guard.pending("a1")["messages"] == 0


def test_cumack_is_accounted_per_path():
    guard = SctpStreamGuard()
    guard.register_association("a1")
    guard.add_path("a1", "p1", primary=True)
    guard.add_path("a1", "p2")
    first = guard.send("a1", 0, b"one")
    second = guard.send("a1", 0, b"two")
    guard.cumack("a1", first["tsn"], "p1")
    assert guard.unacked_tsns("a1", "p1") == []
    assert guard.unacked_tsns("a1", "p2") == [first["tsn"], second["tsn"]]


def test_heartbeat_timeout_marks_only_one_path_down():
    guard = SctpStreamGuard()
    guard.register_association("a1")
    guard.add_path("a1", "p1", primary=True)
    guard.add_path("a1", "p2")
    result = guard.heartbeat_timeout("a1", "p2")
    assert result["association_alive"] is True
    assert guard.path_state("a1", "p1") is True
    assert guard.path_state("a1", "p2") is False


def test_auth_failure_aborts_association_with_cause():
    guard = SctpStreamGuard()
    guard.register_association("a1")
    with pytest.raises(SctpError) as exc:
        guard.authenticate("a1", b"auth-chunk", valid=False)
    assert exc.value.code == "ABORT"
    assert exc.value.cause == "AUTH_FAILURE"
    assert guard.association_closed("a1") is True
    with pytest.raises(SctpError) as send_exc:
        guard.send("a1", 0, b"after-abort")
    assert send_exc.value.code == "ENOTCONN"


def test_zero_receive_window_stops_sending():
    guard = SctpStreamGuard()
    guard.register_association("a1")
    before = guard.pending("a1")["bytes"]
    guard.set_rwnd("a1", 0, 0)
    with pytest.raises(SctpError) as exc:
        guard.send("a1", 0, b"blocked")
    assert exc.value.code == "EWOULDBLOCK"
    assert guard.pending("a1")["bytes"] == before


def test_restart_preserves_unacked_tsn_and_stream_state():
    guard = SctpStreamGuard()
    guard.register_association("a1")
    guard.send("a1", 0, b"one")
    guard.send("a1", 0, b"two")
    saved = guard.snapshot("a1")
    restored = SctpStreamGuard()
    restored.restore("a1", saved)
    assert restored.next_ssn("a1", 0) == 2
    assert len(restored.unacked_tsns("a1")) == 2
    resent = restored.send("a1", 0, b"three")
    assert resent["ssn"] == 2


def test_peer_stream_negotiation_rejects_out_of_range_stream():
    guard = SctpStreamGuard()
    guard.register_association("a1", peer_streams=8)
    guard.send("a1", 7, b"last-valid")
    with pytest.raises(SctpError) as exc:
        guard.send("a1", 8, b"too-far")
    assert exc.value.code == "EINVAL"


def test_path_failover_selects_reachable_path():
    guard = SctpStreamGuard()
    guard.register_association("a1")
    guard.add_path("a1", "p1", primary=True)
    guard.add_path("a1", "p2")
    assert guard.active_path("a1") == "p1"
    guard.set_path_state("a1", "p1", False)
    assert guard.active_path("a1") == "p2"


def test_reassembly_timeout_releases_buffer():
    guard = SctpStreamGuard()
    guard.register_association("a1")
    guard.receive_chunk("a1", 0, 1, 0, b"12345", last=False)
    assert guard.reassembly_bytes("a1", 0) == 5
    guard.sweep_reassembly(now=10000)
    assert guard.reassembly_bytes("a1", 0) == 0


def test_route_lookup_selects_matching_source_address():
    guard = SctpStreamGuard()
    guard.register_association("a1")
    guard.add_route("10.0.0.0/8", "192.0.2.10")
    guard.add_route("0.0.0.0/0", "198.51.100.10")
    assert guard.select_source("a1", "10.1.2.3") == "192.0.2.10"
    assert guard.select_source("a1", "203.0.113.9") == "198.51.100.10"


def test_duplicate_chunk_is_dropped_and_counted():
    guard = SctpStreamGuard()
    guard.register_association("a1")
    first = guard.receive_chunk("a1", 0, 7, 3, b"data", last=True)
    duplicate = guard.receive_chunk("a1", 0, 7, 3, b"data", last=True)
    assert first["duplicate"] is False
    assert duplicate["duplicate"] is True
    assert duplicate["payload"] == b""
    assert guard.duplicate_count("a1", 0) == 1
