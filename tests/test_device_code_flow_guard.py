import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from device_code_flow_guard import DeviceCodeError, DeviceCodeGate
import device_code_flow_guard


def make_gate():
    gate = DeviceCodeGate(interval=5, expires_in=600)
    gate.register_client("tv", name="TV App", scopes=["read", "write"])
    gate.register_client("other", name="Other App", scopes=["read"])
    return gate


def test_user_codes_are_random_not_sequential():
    gate = make_gate()
    codes = [gate.start("tv", "read")["user_code"] for _ in range(100)]
    assert len(set(codes)) == 100
    assert all(len(code) >= 8 for code in codes)
    assert all(code.isalnum() and code == code.upper() for code in codes)


def test_too_fast_poll_returns_slow_down_and_increases_interval():
    gate = make_gate()
    started = gate.start("tv", "read")
    first = gate.poll(started["device_code"], "tv", now=0.0, client_ip="192.0.2.1")
    assert first["status"] == "pending"
    second = gate.poll(started["device_code"], "tv", now=1.0, client_ip="192.0.2.1")
    assert second["status"] == "slow_down"
    assert second["interval"] > first["interval"]
    third = gate.poll(started["device_code"], "tv", now=12.0, client_ip="192.0.2.1")
    assert third["status"] == "pending"


def test_expired_device_code_poll_returns_expired():
    gate = make_gate()
    started = gate.start("tv", "read")
    result = gate.poll(started["device_code"], "tv", now=601.0)
    assert result["status"] == "expired"


def test_concurrent_approval_succeeds_only_once():
    gate = make_gate()
    started = gate.start("tv", "read")
    barrier = threading.Barrier(2)

    def worker(user_id):
        barrier.wait()
        try:
            gate.approve(started["user_code"], user_id, now=0.0)
        except DeviceCodeError as exc:
            return exc.code
        return "ok"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(worker, ["alice", "bob"]))
    assert results.count("ok") == 1
    assert results.count("already_approved") == 1


def test_anonymous_approval_is_rejected():
    gate = make_gate()
    started = gate.start("tv", "read")
    with pytest.raises(DeviceCodeError) as exc:
        gate.approve(started["user_code"], "", now=0.0)
    assert exc.value.code == "login_required"


def test_issued_token_scope_matches_requested_scope():
    gate = make_gate()
    started = gate.start("tv", "read")
    gate.approve(started["user_code"], "alice", now=0.0)
    result = gate.poll(started["device_code"], "tv", now=10.0)
    assert result["status"] == "token"
    assert result["scope"] == "read"


def test_approval_cannot_escalate_scope():
    gate = make_gate()
    started = gate.start("tv", "read")
    with pytest.raises(DeviceCodeError) as exc:
        gate.approve(
            started["user_code"], "alice", scope=["read", "write"], now=0.0
        )
    assert exc.value.code == "invalid_scope"


def test_user_denial_invalidates_immediately():
    gate = make_gate()
    started = gate.start("tv", "read")
    gate.deny(started["user_code"], "alice", now=0.0)
    result = gate.poll(started["device_code"], "tv", now=10.0)
    assert result["status"] == "denied"


def test_poll_rate_limit_is_keyed_by_device_code_not_ip():
    gate = make_gate()
    started = gate.start("tv", "read")
    gate.poll(started["device_code"], "tv", now=0.0, client_ip="192.0.2.1")
    result = gate.poll(started["device_code"], "tv", now=1.0, client_ip="198.51.100.9")
    assert result["status"] == "slow_down"


def test_restart_preserves_pending_device_flow():
    gate = make_gate()
    started = gate.start("tv", "read")
    snapshot = gate.snapshot()
    restored = DeviceCodeGate(interval=5, expires_in=600)
    restored.restore(snapshot)
    restored.approve(started["user_code"], "alice", now=0.0)
    result = restored.poll(started["device_code"], "tv", now=10.0)
    assert result["status"] == "token"


def test_device_code_is_bound_to_requesting_client():
    gate = make_gate()
    started = gate.start("tv", "read")
    with pytest.raises(DeviceCodeError) as exc:
        gate.poll(started["device_code"], "other", now=0.0)
    assert exc.value.code == "invalid_grant"


def test_token_cannot_be_collected_twice():
    gate = make_gate()
    started = gate.start("tv", "read")
    gate.approve(started["user_code"], "alice", now=0.0)
    assert gate.poll(started["device_code"], "tv", now=10.0)["status"] == "token"
    with pytest.raises(DeviceCodeError) as exc:
        gate.poll(started["device_code"], "tv", now=20.0)
    assert exc.value.code == "invalid_grant"


def test_audit_links_user_code_and_client_id():
    gate = make_gate()
    started = gate.start("tv", "read")
    gate.approve(started["user_code"], "alice", now=0.0)
    events = gate.audit_events()
    assert any(
        event.get("user_code") == started["user_code"]
        and event.get("client_id") == "tv"
        for event in events
    )


def test_expired_user_code_cannot_be_approved():
    gate = make_gate()
    started = gate.start("tv", "read")
    with pytest.raises(DeviceCodeError) as exc:
        gate.approve(started["user_code"], "alice", now=601.0)
    assert exc.value.code == "expired"


def test_describe_shows_application_and_requested_scope():
    gate = make_gate()
    started = gate.start("tv", "read")
    description = gate.describe(started["user_code"])
    assert description["application"] == "TV App"
    assert description["scope"] == "read"
    assert description["client_id"] == "tv"
