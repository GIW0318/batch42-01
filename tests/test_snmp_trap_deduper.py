import hashlib
import hmac

import pytest

import snmp_trap_deduper
from snmp_trap_deduper import (
    SnmpAuthError,
    SnmpError,
    SnmpFormatError,
    SnmpTrapDeduper,
)

SECRET = b"snmp-secret"


def _trap(**overrides):
    trap = {
        "engine": "80000001",
        "context": "ctx1",
        "oid": "1.3.6.1.6.3.1.1.5.3",
        "varbinds": [("1.3.6.1.2.1.1.3.0", 100)],
        "uptime": 1000,
        "request_id": 1,
        "kind": "trap",
        "received_at": 0,
    }
    trap.update(overrides)
    return trap


def test_same_oid_different_engine_not_deduped():
    deduper = SnmpTrapDeduper()
    assert deduper.ingest(_trap(engine="80000001")) is True
    assert deduper.ingest(_trap(engine="80000002")) is True
    assert deduper.cache_size() == 2


def test_different_context_not_deduped():
    deduper = SnmpTrapDeduper()
    assert deduper.ingest(_trap(context="ctx1")) is True
    assert deduper.ingest(_trap(context="ctx2")) is True
    assert deduper.cache_size() == 2


def test_varbind_order_canonicalized():
    deduper = SnmpTrapDeduper()
    first = _trap(varbinds=[("1.3.6.1.2.1.1.3.0", 100), ("1.3.6.1.2.1.1.5.0", "sw1")])
    second = _trap(varbinds=[("1.3.6.1.2.1.1.5.0", "sw1"), ("1.3.6.1.2.1.1.3.0", 100)])
    assert deduper.dedup_key(first) == deduper.dedup_key(second)
    assert deduper.ingest(first) is True
    assert deduper.ingest(second) is False


def test_inform_retransmit_deduped_by_request_id():
    deduper = SnmpTrapDeduper()
    trap = _trap(kind="inform", request_id=77)
    assert deduper.ingest(trap) is True
    assert deduper.ingest(dict(trap)) is False
    assert deduper.stats()["duplicates"] == 1


def test_inform_different_request_id_stored():
    deduper = SnmpTrapDeduper()
    assert deduper.ingest(_trap(kind="inform", request_id=1)) is True
    assert deduper.ingest(_trap(kind="inform", request_id=2)) is True
    assert deduper.cache_size() == 2


def test_outside_window_discarded_and_counted():
    deduper = SnmpTrapDeduper(window_seconds=300)
    assert deduper.ingest(_trap(received_at=0), now=301) is False
    assert deduper.cache_size() == 0
    assert deduper.stats()["discarded"] == 1


def test_uptime_wrap_sorting():
    deduper = SnmpTrapDeduper()
    traps = [_trap(uptime=100), _trap(uptime=0xFFFFFFF0), _trap(uptime=50)]
    ordered = [item["uptime"] for item in deduper.sort_by_uptime(traps)]
    assert ordered == [0xFFFFFFF0, 50, 100]


def test_verify_v3_requires_full_digest():
    deduper = SnmpTrapDeduper()
    trap = _trap()
    canonical = "80000001|ctx1|1.3.6.1.6.3.1.1.5.3|1|1000"
    digest = hmac.new(SECRET, canonical.encode(), hashlib.sha256).hexdigest()
    full = dict(trap, digest=digest)
    assert deduper.verify_v3(full, SECRET) is True
    truncated = dict(trap, digest=digest[:16])
    with pytest.raises(SnmpAuthError) as exc:
        deduper.verify_v3(truncated, SECRET)
    assert exc.value.code == "auth-failed"


def test_engine_discovery_rejects_unknown():
    deduper = SnmpTrapDeduper(known_engines={"80000001"})
    assert deduper.ingest(_trap(engine="80000001")) is True
    with pytest.raises(SnmpError) as exc:
        deduper.ingest(_trap(engine="8000FFFF"))
    assert exc.value.code == "unknown-engine"


def test_severity_unknown_is_not_critical():
    deduper = SnmpTrapDeduper(
        severity_map={"1.3.6.1.6.3.1.1.5.3": "major"}
    )
    assert deduper.severity("1.3.6.1.6.3.1.1.5.3") == "major"
    assert deduper.severity("1.3.6.1.4.1.999.1") == "unknown"


def test_batch_rolls_back_on_malformed_entry():
    deduper = SnmpTrapDeduper()
    batch = [_trap(request_id=1), _trap(request_id=2), {"engine": "80000001"}]
    with pytest.raises(SnmpFormatError) as exc:
        deduper.ingest_batch(batch)
    assert exc.value.code == "format-error"
    assert deduper.cache_size() == 0
    assert deduper.stats()["stored"] == 0


def test_cache_bounded_eviction():
    deduper = SnmpTrapDeduper(max_cache=2)
    deduper.ingest(_trap(request_id=1))
    deduper.ingest(_trap(request_id=2))
    deduper.ingest(_trap(request_id=3))
    assert deduper.cache_size() == 2


def test_snapshot_restore_keeps_dedup_window():
    deduper = SnmpTrapDeduper()
    trap = _trap()
    deduper.ingest(trap)
    snapshot = deduper.snapshot()
    restored = SnmpTrapDeduper()
    restored.restore(snapshot)
    assert restored.ingest(dict(trap)) is False
    assert restored.stats()["duplicates"] == 1


def test_auth_and_format_errors_are_distinct():
    deduper = SnmpTrapDeduper()
    with pytest.raises(SnmpAuthError) as auth_exc:
        deduper.verify_v3(_trap(digest="00"), SECRET)
    with pytest.raises(SnmpFormatError) as format_exc:
        deduper.ingest({"engine": "80000001"})
    assert auth_exc.value.code == "auth-failed"
    assert format_exc.value.code == "format-error"
    assert not isinstance(format_exc.value, SnmpAuthError)


def test_duplicate_counter_increments():
    deduper = SnmpTrapDeduper()
    trap = _trap()
    assert deduper.ingest(trap) is True
    assert deduper.ingest(dict(trap)) is False
    assert deduper.stats()["stored"] == 1
    assert deduper.stats()["duplicates"] == 1
