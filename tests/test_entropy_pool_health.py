import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from entropy_pool_health import EntropyError, EntropyHealthMonitor
import entropy_pool_health


def ready_monitor():
    monitor = EntropyHealthMonitor(pool_size=256)
    monitor.add_source("software", weight=4.0)
    return monitor


def test_uninitialized_nonblocking_read_returns_eagain():
    monitor = EntropyHealthMonitor()
    with pytest.raises(EntropyError) as exc:
        monitor.read(16, blocking=False)
    assert exc.value.code == "EAGAIN"


def test_uninitialized_blocking_read_still_returns_eagain():
    monitor = EntropyHealthMonitor()
    with pytest.raises(EntropyError) as exc:
        monitor.read(16, blocking=True)
    assert exc.value.code == "EAGAIN"


def test_pool_becomes_ready_only_when_estimate_reaches_size():
    monitor = EntropyHealthMonitor(pool_size=256)
    monitor.add_source("slow", weight=1.0)
    assert monitor.estimate() < 256
    assert monitor.ready() is False
    monitor.add_source("fast-a", weight=1.0)
    monitor.add_source("fast-b", weight=1.0)
    monitor.add_source("fast-c", weight=1.0)
    assert monitor.estimate() >= 256
    assert monitor.ready() is True


def test_vm_restore_from_same_snapshot_produces_different_output():
    base = ready_monitor()
    snapshot = base.snapshot()
    first = EntropyHealthMonitor()
    second = EntropyHealthMonitor()
    first.vm_restore(snapshot)
    second.vm_restore(snapshot)
    assert first.read(32) != second.read(32)


def test_restored_seed_makes_pool_ready():
    monitor = EntropyHealthMonitor()
    with pytest.raises(EntropyError):
        monitor.read(8, blocking=False)
    monitor.restore_seed(b"S" * 32)
    assert monitor.ready() is True
    assert len(monitor.read(8)) == 8


def test_user_namespace_cannot_read_seed():
    monitor = ready_monitor()
    with pytest.raises(EntropyError) as exc:
        monitor.seed(caller_ns="user")
    assert exc.value.code == "EACCES"


def test_hardware_failure_falls_back_to_software_source():
    monitor = EntropyHealthMonitor(pool_size=128)
    monitor.add_source("hwrng", kind="hardware", weight=2.0)
    monitor.add_source("software", weight=2.0)
    monitor.set_source_health("hwrng", False)
    assert monitor.ready() is True
    assert len(monitor.read(16)) == 16


def test_degraded_status_is_visible():
    monitor = EntropyHealthMonitor(pool_size=128)
    monitor.add_source("hwrng", kind="hardware", weight=2.0)
    monitor.add_source("software", weight=2.0)
    monitor.set_source_health("hwrng", False)
    status = monitor.status()
    assert status["degraded"] is True
    assert "software" in status["active_sources"]
    assert "hwrng" in status["failed_sources"]


def test_source_health_change_is_audited():
    monitor = EntropyHealthMonitor()
    monitor.add_source("hwrng", kind="hardware", weight=1.0)
    monitor.set_source_health("hwrng", False)
    events = monitor.audit_events()
    assert any(
        event["source"] == "hwrng" and event["healthy"] is False
        for event in events
    )


def test_weighted_source_shares_are_normalized():
    monitor = EntropyHealthMonitor()
    monitor.add_source("fast", weight=3.0)
    monitor.add_source("slow", weight=1.0)
    shares = monitor.source_shares()
    assert shares["fast"] == pytest.approx(0.75)
    assert shares["slow"] == pytest.approx(0.25)


def test_concurrent_reads_never_return_duplicate_blocks():
    monitor = ready_monitor()
    barrier = threading.Barrier(8)

    def worker(_):
        barrier.wait()
        return b"".join(monitor.read(16) for _ in range(25))

    with ThreadPoolExecutor(max_workers=8) as pool:
        blocks = list(pool.map(worker, range(8)))
    assert len(set(blocks)) == 8


def test_closed_monitor_refuses_reads():
    monitor = ready_monitor()
    monitor.close()
    with pytest.raises(EntropyError) as exc:
        monitor.read(16)
    assert exc.value.code == "ECLOSED"


def test_drained_pool_returns_eagain():
    monitor = ready_monitor()
    monitor.drain()
    with pytest.raises(EntropyError) as exc:
        monitor.read(16, blocking=False)
    assert exc.value.code == "EAGAIN"


def test_snapshot_carries_persisted_seed():
    monitor = ready_monitor()
    snapshot = monitor.snapshot()
    restored = EntropyHealthMonitor()
    restored.restore_seed(snapshot["seed"])
    assert restored.ready() is True
    assert len(restored.read(32)) == 32


def test_read_returns_requested_length_and_nonzero_material():
    monitor = ready_monitor()
    block = monitor.read(64)
    assert len(block) == 64
    assert any(byte != 0 for byte in block)
