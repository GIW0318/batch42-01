import hashlib
import hmac

import pytest

import ldp_label_ledger
from ldp_label_ledger import LdpError, LdpLabelLedger

SECRET = b"ldp-secret"


def _ledger(**kwargs):
    ledger = LdpLabelLedger(secret=SECRET, **kwargs)
    ledger.open_session("peer1", "192.0.2.1:646")
    return ledger


def _digest(payload):
    return hmac.new(SECRET, payload, hashlib.sha256).hexdigest()


def test_authenticate_verifies_every_message():
    ledger = _ledger()
    payload = b"label-mapping"
    assert ledger.authenticate("peer1", 1, payload, _digest(payload)) is True
    bad = list(_digest(payload))
    bad[-1] = "0" if bad[-1] != "0" else "1"
    with pytest.raises(LdpError) as exc:
        ledger.authenticate("peer1", 2, payload + b"x", "".join(bad))
    assert exc.value.code == "auth-failed"


def test_duplicate_advertise_same_seq_idempotent():
    ledger = _ledger()
    first = ledger.advertise("peer1", "fec-a", 100, 1, local_pref=100, metric=10)
    second = ledger.advertise("peer1", "fec-a", 100, 1, local_pref=100, metric=10)
    assert first["label"] == second["label"] == 100
    assert ledger.route("fec-a")["peer"] == "peer1"
    assert ledger.route("fec-a")["label"] == 100


def test_stale_advertise_rejected():
    ledger = _ledger()
    ledger.advertise("peer1", "fec-a", 100, 5)
    with pytest.raises(LdpError) as exc:
        ledger.advertise("peer1", "fec-a", 101, 4)
    assert exc.value.code == "stale-sequence"
    assert ledger.route("fec-a")["label"] == 100


def test_withdraw_removes_route_and_label():
    ledger = _ledger()
    ledger.advertise("peer1", "fec-a", 100, 1)
    assert ledger.withdraw("peer1", "fec-a", 2) is True
    assert ledger.route("fec-a") is None
    assert ledger.label_in_use(100) is False


def test_duplicate_withdraw_idempotent():
    ledger = _ledger()
    ledger.advertise("peer1", "fec-a", 100, 1)
    ledger.withdraw("peer1", "fec-a", 2)
    assert ledger.withdraw("peer1", "fec-a", 2) is True
    with pytest.raises(LdpError) as exc:
        ledger.withdraw("peer1", "fec-a", 1)
    assert exc.value.code == "stale-sequence"


def test_best_path_by_local_pref_then_metric():
    ledger = _ledger()
    ledger.open_session("peer2", "192.0.2.2:646")
    ledger.open_session("peer3", "192.0.2.3:646")
    ledger.advertise("peer1", "fec-a", 100, 1, local_pref=100, metric=10)
    ledger.advertise("peer2", "fec-a", 200, 1, local_pref=200, metric=50)
    ledger.advertise("peer3", "fec-a", 300, 1, local_pref=200, metric=20)
    route = ledger.route("fec-a")
    assert route["peer"] == "peer3"
    assert route["label"] == 300


def test_session_down_reclaims_labels():
    ledger = _ledger()
    ledger.advertise("peer1", "fec-a", 16, 1)
    assert ledger.label_in_use(16) is True
    released = ledger.session_down("peer1")
    assert released == [16]
    assert ledger.label_in_use(16) is False
    assert ledger.route("fec-a") is None
    ledger.open_session("peer2", "192.0.2.2:646")
    assert ledger.request_label("peer2", "fec-b") == 16


def test_label_pool_exhaustion_error():
    ledger = LdpLabelLedger(secret=SECRET, label_min=16, label_max=17)
    ledger.open_session("peer1", "192.0.2.1:646")
    ledger.advertise("peer1", "fec-a", 16, 1)
    ledger.advertise("peer1", "fec-b", 17, 1)
    with pytest.raises(LdpError) as exc:
        ledger.request_label("peer1", "fec-c")
    assert exc.value.code == "resource-exhausted"


def test_invalid_label_rejected():
    ledger = _ledger(label_min=16, label_max=100)
    with pytest.raises(LdpError) as exc:
        ledger.advertise("peer1", "fec-a", 0, 1)
    assert exc.value.code == "invalid-label"
    with pytest.raises(LdpError) as exc:
        ledger.advertise("peer1", "fec-a", 101, 1)
    assert exc.value.code == "invalid-label"


def test_released_label_quarantined_then_reusable():
    ledger = _ledger(label_min=16, label_max=18, quarantine_seconds=30)
    first = ledger.request_label("peer1", "fec-a")
    assert first == 16
    ledger.release_label("peer1", first, 1)
    second = ledger.request_label("peer1", "fec-b")
    assert second != first
    ledger.advance(31)
    assert ledger.request_label("peer1", "fec-c") == 16


def test_transport_change_clears_session_routes():
    ledger = _ledger()
    ledger.advertise("peer1", "fec-a", 100, 1)
    assert ledger.set_transport("peer1", "198.51.100.1:646") is True
    assert ledger.transport_of("peer1") == "198.51.100.1:646"
    assert ledger.route("fec-a") is None


def test_ttl_zero_blocks_forwarding():
    ledger = _ledger()
    assert ledger.check_ttl(5) == 4
    with pytest.raises(LdpError) as exc:
        ledger.check_ttl(1)
    assert exc.value.code == "ttl-exceeded"
    with pytest.raises(LdpError):
        ledger.check_ttl(0)


def test_fatal_notification_clears_session():
    ledger = _ledger()
    ledger.advertise("peer1", "fec-a", 100, 1)
    assert ledger.handle_notification("peer1", 20) == "warning"
    assert ledger.route("fec-a") is not None
    assert ledger.handle_notification("peer1", 90) == "fatal"
    assert ledger.route("fec-a") is None


def test_graceful_restart_reconcile():
    ledger = _ledger()
    ledger.advertise("peer1", "fec-a", 100, 1)
    ledger.advertise("peer1", "fec-b", 101, 1)
    snapshot = ledger.snapshot()
    restored = LdpLabelLedger(secret=SECRET)
    restored.restore(snapshot)
    report = restored.reconcile("peer1", ["fec-a", "fec-c"])
    assert report["missing"] == ["fec-b"]
    assert report["extra"] == ["fec-c"]


def test_snapshot_restore_pool_no_collision():
    ledger = _ledger(label_min=16, label_max=17)
    ledger.advertise("peer1", "fec-a", 16, 1)
    snapshot = ledger.snapshot()
    restored = LdpLabelLedger(secret=SECRET, label_min=16, label_max=17)
    restored.restore(snapshot)
    assert restored.label_in_use(16) is True
    assert restored.request_label("peer2", "fec-b") == 17
    with pytest.raises(LdpError) as exc:
        restored.request_label("peer2", "fec-c")
    assert exc.value.code == "resource-exhausted"
