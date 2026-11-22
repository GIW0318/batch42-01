import base64
import hashlib
import hmac
import json
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from mfa_enrollment_ledger import MfaEnrollmentLedger, MfaError
import mfa_enrollment_ledger


def totp(secret, now, step=30, digits=6):
    padding = "=" * ((8 - len(secret) % 8) % 8)
    key = base64.b32decode(secret + padding)
    counter = int(now // step)
    digest = hmac.new(key, counter.to_bytes(8, "big"), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    value = int.from_bytes(digest[offset:offset + 4], "big") & 0x7FFFFFFF
    return str(value % (10 ** digits)).zfill(digits)


def enroll(ledger, user, device, now=0.0):
    info = ledger.begin_enrollment(user, device)
    code = totp(info["secret"], now)
    ledger.confirm_enrollment(user, device, code, now=now)
    return info


def test_concurrent_binding_keeps_only_one_active_authenticator():
    ledger = MfaEnrollmentLedger()
    enroll(ledger, "alice", "phone-1")
    with pytest.raises(MfaError) as exc:
        ledger.begin_enrollment("alice", "phone-2")
    assert exc.value.code == "EALREADY"
    assert len(ledger.enrollments("alice")) == 1
    assert ledger.enrollments("alice")[0]["device_id"] == "phone-1"


def test_confirm_code_attempts_are_locked_out():
    ledger = MfaEnrollmentLedger(max_confirm_attempts=3)
    info = ledger.begin_enrollment("alice", "phone-1")
    real = totp(info["secret"], 0.0)
    wrong = "%06d" % ((int(real) + 1) % 1000000)
    for _ in range(3):
        with pytest.raises(MfaError) as exc:
            ledger.confirm_enrollment("alice", "phone-1", wrong, now=0.0)
        assert exc.value.code == "INVALID_CODE"
    with pytest.raises(MfaError) as exc:
        ledger.confirm_enrollment("alice", "phone-1", real, now=0.0)
    assert exc.value.code == "ELOCKED"


def test_backup_codes_are_hashed_at_rest():
    ledger = MfaEnrollmentLedger()
    codes = ledger.generate_backup_codes("alice")
    assert len(codes) >= 5
    text = json.dumps(ledger.snapshot(), default=str)
    for code in codes:
        assert code not in text


def test_refreshing_pending_enrollment_does_not_half_bind():
    ledger = MfaEnrollmentLedger()
    first = ledger.begin_enrollment("alice", "phone-1")
    second = ledger.begin_enrollment("alice", "phone-1")
    assert second["secret"] == first["secret"]
    assert second["state"] == "pending"
    assert ledger.enrollments("alice") == []
    code = totp(first["secret"], 0.0)
    assert ledger.confirm_enrollment("alice", "phone-1", code, now=0.0)["state"] == "active"


def test_recovery_without_mfa_is_rejected():
    ledger = MfaEnrollmentLedger()
    enroll(ledger, "alice", "phone-1")
    with pytest.raises(MfaError) as exc:
        ledger.recover("support", "alice", id_number="123456", mfa_ok=False)
    assert exc.value.code == "MFA_REQUIRED"
    assert len(ledger.enrollments("alice")) == 1


def test_removed_device_secret_is_invalid_immediately():
    ledger = MfaEnrollmentLedger()
    info = enroll(ledger, "alice", "phone-1")
    ledger.remove_device("alice", "phone-1")
    code = totp(info["secret"], 0.0)
    with pytest.raises(MfaError) as exc:
        ledger.verify("alice", code, device_id="phone-1", now=0.0)
    assert exc.value.code == "UNKNOWN_DEVICE"


def test_totp_window_rejects_far_future_code():
    ledger = MfaEnrollmentLedger(totp_window=1)
    info = enroll(ledger, "alice", "phone-1")
    far_future = totp(info["secret"], 300.0)
    with pytest.raises(MfaError) as exc:
        ledger.verify("alice", far_future, device_id="phone-1", now=0.0)
    assert exc.value.code == "INVALID_CODE"


def test_totp_window_accepts_previous_step():
    ledger = MfaEnrollmentLedger(totp_window=1)
    info = enroll(ledger, "alice", "phone-1")
    previous = totp(info["secret"], 30.0)
    result = ledger.verify("alice", previous, device_id="phone-1", now=60.0)
    assert result["ok"] is True


def test_same_totp_code_is_replay_rejected():
    ledger = MfaEnrollmentLedger()
    info = enroll(ledger, "alice", "phone-1")
    code = totp(info["secret"], 30.0)
    ledger.verify("alice", code, device_id="phone-1", now=30.0)
    with pytest.raises(MfaError) as exc:
        ledger.verify("alice", code, device_id="phone-1", now=31.0)
    assert exc.value.code == "REPLAY"


def test_concurrent_same_code_succeeds_once():
    ledger = MfaEnrollmentLedger()
    info = enroll(ledger, "alice", "phone-1")
    code = totp(info["secret"], 0.0)
    barrier = threading.Barrier(8)

    def worker(_):
        barrier.wait()
        try:
            ledger.verify("alice", code, device_id="phone-1", now=0.0)
        except MfaError as exc:
            return exc.code
        return "ok"

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(worker, range(8)))
    assert results.count("ok") == 1
    assert results.count("REPLAY") == 7


def test_grace_period_cannot_be_reset():
    ledger = MfaEnrollmentLedger(grace_days=7)
    ledger.activate_user("alice", now=0.0)
    assert ledger.grace_active("alice", now=1.0) is True
    with pytest.raises(MfaError) as exc:
        ledger.reset_grace("alice")
    assert exc.value.code == "GRACE_ALREADY_USED"
    assert ledger.grace_active("alice", now=2.0) is True


def test_changing_phone_invalidates_the_old_backup_channel():
    ledger = MfaEnrollmentLedger()
    ledger.set_phone("alice", "13800000001")
    ledger.change_phone("alice", "13800000002")
    assert ledger.backup_phone("alice") == "13800000002"
    assert ledger.can_send_to("alice", "13800000001") is False
    assert ledger.can_send_to("alice", "13800000002") is True


def test_backup_code_is_single_use():
    ledger = MfaEnrollmentLedger()
    codes = ledger.generate_backup_codes("alice")
    assert ledger.use_backup_code("alice", codes[0]) is True
    assert ledger.use_backup_code("alice", codes[0]) is False


def test_restart_restores_pending_enrollment():
    ledger = MfaEnrollmentLedger()
    info = ledger.begin_enrollment("alice", "phone-1")
    restored = MfaEnrollmentLedger()
    restored.restore(ledger.snapshot())
    code = totp(info["secret"], 0.0)
    result = restored.confirm_enrollment("alice", "phone-1", code, now=0.0)
    assert result["state"] == "active"
    assert len(restored.enrollments("alice")) == 1


def test_enrollment_audit_records_operator():
    ledger = MfaEnrollmentLedger()
    info = ledger.begin_enrollment("alice", "phone-1", actor="admin")
    code = totp(info["secret"], 0.0)
    ledger.confirm_enrollment("alice", "phone-1", code, now=0.0, actor="admin")
    events = ledger.audit_events()
    assert events
    assert events[-1]["actor"] == "admin"
    assert events[-1]["device_id"] == "phone-1"
