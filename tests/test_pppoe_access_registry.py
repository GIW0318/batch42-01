import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from pppoe_access_registry import PppoeAccessRegistry, PppoeError
import pppoe_access_registry


def test_concurrent_dial_same_mac_creates_one_session():
    registry = PppoeAccessRegistry()
    barrier = threading.Barrier(12)

    def worker(index):
        barrier.wait()
        try:
            registry.dial("aa:bb:cc:dd:ee:ff", "sess-%d" % index, "acct-1")
        except PppoeError as exc:
            return exc.code
        return "ok"

    with ThreadPoolExecutor(max_workers=12) as pool:
        results = list(pool.map(worker, range(12)))
    assert results.count("ok") == 1
    assert results.count("EALREADY") == 11
    assert len(registry.account_sessions("acct-1")) == 1


def test_duplicate_mac_with_new_session_is_rejected():
    registry = PppoeAccessRegistry()
    registry.dial("aa:bb:cc:dd:ee:01", "s1", "acct-1")
    with pytest.raises(PppoeError) as exc:
        registry.dial("aa:bb:cc:dd:ee:01", "s2", "acct-2")
    assert exc.value.code == "EALREADY"


def test_reused_session_id_stale_disconnect_is_ignored():
    registry = PppoeAccessRegistry()
    first = registry.dial("aa:bb:cc:dd:ee:02", "s1", "acct-1")
    assert registry.disconnect("aa:bb:cc:dd:ee:02", "s1", first["epoch"]) is True
    second = registry.dial("aa:bb:cc:dd:ee:02", "s1", "acct-1")
    assert second["epoch"] != first["epoch"]
    assert registry.disconnect("aa:bb:cc:dd:ee:02", "s1", first["epoch"]) is False
    assert len(registry.account_sessions("acct-1")) == 1


def test_restart_restores_online_table_without_double_billing():
    registry = PppoeAccessRegistry(session_fee=1.0)
    registry.dial("aa:bb:cc:dd:ee:03", "s1", "acct-1")
    registry.dial("aa:bb:cc:dd:ee:04", "s2", "acct-2")
    saved = registry.snapshot()
    restored = PppoeAccessRegistry(session_fee=1.0)
    restored.restore(saved)
    assert restored.accounting_total("acct-1") == 1.0
    assert restored.accounting_total("acct-2") == 1.0
    with pytest.raises(PppoeError) as exc:
        restored.dial("aa:bb:cc:dd:ee:03", "s3", "acct-1")
    assert exc.value.code == "EALREADY"
    assert restored.accounting_total("acct-1") == 1.0


def test_late_auth_response_is_rejected():
    registry = PppoeAccessRegistry()
    registry.dial("aa:bb:cc:dd:ee:05", "s1", "acct-1")
    with pytest.raises(PppoeError) as exc:
        registry.authenticate(
            "aa:bb:cc:dd:ee:05", "s1", ok=True, started_at=0.0, now=31.0
        )
    assert exc.value.code == "ETIMEDOUT"
    assert registry.account_sessions("acct-1")[0]["auth_state"] == "pending"


def test_idle_check_does_not_disconnect_active_download():
    registry = PppoeAccessRegistry(idle_timeout=300)
    registry.dial("aa:bb:cc:dd:ee:06", "s1", "acct-1")
    registry.authenticate(
        "aa:bb:cc:dd:ee:06", "s1", ok=True, started_at=0.0, now=0.0
    )
    registry.record_traffic("aa:bb:cc:dd:ee:06", 1024, now=100.0)
    assert registry.idle_check(390.0) == []
    assert len(registry.account_sessions("acct-1")) == 1
    assert registry.idle_check(401.0) == ["aa:bb:cc:dd:ee:06"]
    assert registry.account_sessions("acct-1") == []


def test_account_session_limit_is_enforced():
    registry = PppoeAccessRegistry(max_sessions_per_account=1)
    registry.dial("aa:bb:cc:dd:ee:07", "s1", "acct-1")
    with pytest.raises(PppoeError) as exc:
        registry.dial("aa:bb:cc:dd:ee:08", "s2", "acct-1")
    assert exc.value.code == "ELIMIT"


def test_mtu_below_1492_is_rejected_instead_of_downgraded():
    registry = PppoeAccessRegistry()
    registry.dial("aa:bb:cc:dd:ee:09", "s1", "acct-1")
    with pytest.raises(PppoeError) as exc:
        registry.set_mtu("aa:bb:cc:dd:ee:09", 1400)
    assert exc.value.code == "EMTU"
    assert registry.set_mtu("aa:bb:cc:dd:ee:09", 1492) == 1492


def test_release_cleans_up_session_handles():
    registry = PppoeAccessRegistry()
    record = registry.dial("aa:bb:cc:dd:ee:0a", "s1", "acct-1")
    assert registry.open_handles() == 1
    assert registry.disconnect("aa:bb:cc:dd:ee:0a", "s1", record["epoch"]) is True
    assert registry.open_handles() == 0


def test_auth_failure_threshold_locks_account():
    registry = PppoeAccessRegistry(auth_failure_limit=3)
    registry.dial("aa:bb:cc:dd:ee:0b", "s1", "acct-1")
    for _ in range(3):
        with pytest.raises(PppoeError) as exc:
            registry.authenticate(
                "aa:bb:cc:dd:ee:0b", "s1", ok=False, started_at=0.0, now=0.0
            )
        assert exc.value.code == "EAUTHFAIL"
    with pytest.raises(PppoeError) as exc:
        registry.authenticate(
            "aa:bb:cc:dd:ee:0b", "s1", ok=True, started_at=0.0, now=1.0
        )
    assert exc.value.code == "ELOCKED"


def test_service_name_matching_is_case_insensitive():
    registry = PppoeAccessRegistry()
    record = registry.dial("aa:bb:cc:dd:ee:0c", "s1", "acct-1", service="IPTV")
    assert record["service"] == "iptv"
    assert registry.normalize_service("Iptv") == "iptv"


def test_bulk_disconnect_retries_failed_stop_accounting():
    registry = PppoeAccessRegistry()
    registry.dial("aa:bb:cc:dd:ee:0d", "s1", "acct-1")
    registry.dial("aa:bb:cc:dd:ee:0e", "s2", "acct-2")
    registry.dial("aa:bb:cc:dd:ee:0f", "s3", "acct-3")
    result = registry.bulk_disconnect(
        ["aa:bb:cc:dd:ee:0d", "aa:bb:cc:dd:ee:0e", "aa:bb:cc:dd:ee:0f"],
        fail_stop=("aa:bb:cc:dd:ee:0e",),
    )
    assert sorted(result["disconnected"]) == ["aa:bb:cc:dd:ee:0d", "aa:bb:cc:dd:ee:0f"]
    assert result["retry"] == ["aa:bb:cc:dd:ee:0e"]
    assert len(registry.account_sessions("acct-2")) == 1
    assert registry.retry_accounting()["retry"] == []
    assert registry.account_sessions("acct-2") == []


def test_disconnect_requires_matching_mac_and_session():
    registry = PppoeAccessRegistry()
    record = registry.dial("aa:bb:cc:dd:ee:10", "s1", "acct-1")
    assert registry.disconnect("aa:bb:cc:dd:ee:99", "s1", record["epoch"]) is False
    assert registry.disconnect("aa:bb:cc:dd:ee:10", "s9", record["epoch"]) is False
    assert len(registry.account_sessions("acct-1")) == 1
    assert registry.disconnect("aa:bb:cc:dd:ee:10", "s1", record["epoch"]) is True


def test_discovery_packets_are_rate_limited():
    registry = PppoeAccessRegistry(discovery_pps=2)
    assert registry.accept_discovery("aa:bb:cc:dd:ee:11", now=0.0) is True
    assert registry.accept_discovery("aa:bb:cc:dd:ee:11", now=0.0) is True
    with pytest.raises(PppoeError) as exc:
        registry.accept_discovery("aa:bb:cc:dd:ee:11", now=0.0)
    assert exc.value.code == "ERATELIMIT"
    assert registry.accept_discovery("aa:bb:cc:dd:ee:11", now=1.0) is True


def test_account_lock_does_not_block_other_accounts():
    registry = PppoeAccessRegistry(auth_failure_limit=1)
    registry.dial("aa:bb:cc:dd:ee:12", "s1", "acct-locked")
    with pytest.raises(PppoeError):
        registry.authenticate(
            "aa:bb:cc:dd:ee:12", "s1", ok=False, started_at=0.0, now=0.0
        )
    registry.dial("aa:bb:cc:dd:ee:13", "s2", "acct-good")
    result = registry.authenticate(
        "aa:bb:cc:dd:ee:13", "s2", ok=True, started_at=0.0, now=0.0
    )
    assert result["auth_state"] == "authenticated"
    assert registry.account_sessions("acct-good")[0]["auth_state"] == "authenticated"
