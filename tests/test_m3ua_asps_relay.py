import threading

import pytest

import m3ua_asps_relay
from m3ua_asps_relay import M3uaAspsRelay, M3uaError


def _relay(**kwargs):
    relay = M3uaAspsRelay(**kwargs)
    relay.add_as("as1", routing_context=1, point_codes=[100])
    relay.add_asp("asp1", "as1")
    return relay


def test_duplicate_point_code_rejected():
    relay = _relay()
    with pytest.raises(M3uaError) as exc:
        relay.add_as("as2", routing_context=2, point_codes=[100])
    assert exc.value.code == "duplicate-point-code"
    assert relay.route_table()["as1"]["point_codes"] == [100]


def test_network_appearance_mismatch_rejected():
    relay = M3uaAspsRelay()
    relay.add_as("as1", routing_context=1, network_appearance=5)
    with pytest.raises(M3uaError) as exc:
        relay.add_asp("asp1", "as1", network_appearance=6)
    assert exc.value.code == "network-appearance-mismatch"
    relay.add_asp("asp2", "as1", network_appearance=5)


def test_routing_context_isolation():
    relay = _relay()
    with pytest.raises(M3uaError) as exc:
        relay.asp_active("asp1", 1, routing_context=2)
    assert exc.value.code == "routing-context-mismatch"
    assert relay.asp_active("asp1", 1, routing_context=1)["active"] is True


def test_duplicate_active_idempotent():
    relay = _relay()
    first = relay.asp_active("asp1", 1)
    second = relay.asp_active("asp1", 1)
    assert first["active"] is True
    assert second["duplicate"] is True
    assert second["active_asps"] == ["asp1"]


def test_inactive_idempotent():
    relay = _relay()
    relay.asp_active("asp1", 1)
    assert relay.asp_inactive("asp1", 2)["active"] is False
    assert relay.asp_inactive("asp1", 2)["duplicate"] is True
    assert relay.active_asp("as1") is None


def test_sequence_unsigned_wrap():
    relay = _relay()
    assert relay.sequence_in_order(0xFFFFFFFE, 1) is True
    assert relay.sequence_in_order(0xFFFFFFFE, 0xFFFFFFFF) is True
    assert relay.sequence_in_order(5, 4) is False


def test_dupu_overrun_reports_field():
    relay = _relay()
    with pytest.raises(M3uaError) as exc:
        relay.handle_dupu(bytes([0, 10, 1, 2]))
    assert exc.value.code == "dupu-overrun"
    assert exc.value.field == "length"
    assert relay.handle_dupu(bytes([0, 2, 1, 2]))["length"] == 2


def test_heartbeat_timeout_fails_over():
    relay = _relay()
    relay.add_asp("asp2", "as1")
    relay.asp_active("asp1", 1)
    result = relay.heartbeat_timeout("asp1")
    assert result["failed_over"] == "asp2"
    assert relay.active_asp("as1") == "asp2"


def test_congestion_blocks_low_priority():
    relay = _relay()
    relay.asp_active("asp1", 1)
    assert relay.parse_aspm(bytes([3]))["congestion"] == 3
    with pytest.raises(M3uaError) as exc:
        relay.send_data("as1", b"low", priority=0)
    assert exc.value.code == "congested"
    assert relay.send_data("as1", b"high", priority=1)


def test_unsupported_service_indicator():
    relay = _relay()
    relay.asp_active("asp1", 1)
    with pytest.raises(M3uaError) as exc:
        relay.send_data("as1", b"data", si=99)
    assert exc.value.code == "unsupported-si"
    assert relay.send_data("as1", b"data", si=3)


def test_queue_limit_explicit():
    relay = _relay(queue_limit=1)
    relay.asp_active("asp1", 1)
    relay.send_data("as1", b"one")
    with pytest.raises(M3uaError) as exc:
        relay.send_data("as1", b"two")
    assert exc.value.code == "queue-full"
    assert relay.queue_size() == 1


def test_low_priority_quota():
    relay = _relay(queue_limit=20, low_priority_quota=2)
    relay.asp_active("asp1", 1)
    for number in range(5):
        relay.send_data("as1", ("high-%d" % number).encode(), priority=1)
    for number in range(5):
        relay.send_data("as1", ("low-%d" % number).encode(), priority=0)
    drained = []
    while True:
        message = relay.next_message()
        if message is None:
            break
        drained.append(message)
    low = [item for item in drained if item["priority"] == 0]
    assert len(low) >= 2
    assert len(drained) == 10


def test_restart_restores_active_state():
    relay = _relay()
    relay.asp_active("asp1", 1)
    snapshot = relay.snapshot()
    restored = M3uaAspsRelay()
    restored.restore(snapshot)
    assert restored.active_asp("as1") == "asp1"
    assert restored.send_data("as1", b"after-restart")
    assert restored.next_message()["payload"] == b"after-restart"


def test_restart_inactive_as_blocks_traffic():
    relay = _relay()
    snapshot = relay.snapshot()
    restored = M3uaAspsRelay()
    restored.restore(snapshot)
    with pytest.raises(M3uaError) as exc:
        restored.send_data("as1", b"data")
    assert exc.value.code == "as-inactive"


def test_unknown_asp_rejected():
    relay = _relay()
    with pytest.raises(M3uaError) as exc:
        relay.asp_active("asp-missing", 1)
    assert exc.value.code == "unknown-asp"
