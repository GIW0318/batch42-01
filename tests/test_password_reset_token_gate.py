import json

import pytest

from password_reset_token_gate import ResetTokenError, ResetTokenGate
import password_reset_token_gate


def setup_gate():
    gate = ResetTokenGate()
    gate.add_account("alice", "alice@example.com")
    return gate


def send_reset(gate, account="alice", now=0.0, ip="192.0.2.10"):
    box = {}

    def mailer(email, token):
        box["email"] = email
        box["token"] = token

    result = gate.request_reset(account, ip=ip, now=now, mailer=mailer)
    return result, box


def test_reset_link_can_only_be_used_once():
    gate = setup_gate()
    _, box = send_reset(gate)
    gate.reset(box["token"], "new-password", now=10.0, ip="192.0.2.10")
    with pytest.raises(ResetTokenError) as exc:
        gate.reset(box["token"], "another-password", now=20.0, ip="192.0.2.10")
    assert exc.value.code == "reused"


def test_password_change_revokes_all_sessions_and_remember_tokens():
    gate = setup_gate()
    gate.add_session("alice")
    gate.add_session("alice")
    gate.add_session("alice")
    gate.add_remember_token("alice")
    gate.add_remember_token("alice")
    _, box = send_reset(gate)
    result = gate.reset(box["token"], "new-password", now=10.0, ip="192.0.2.10")
    assert result["status"] == "ok"
    assert result["revoked_sessions"] == 3
    assert result["revoked_remember"] == 2
    assert gate.active_sessions("alice") == 0
    assert gate.active_remember_tokens("alice") == 0


def test_mail_failure_rolls_back_token_write():
    gate = setup_gate()

    def broken_mailer(email, token):
        raise RuntimeError("smtp down")

    result = gate.request_reset(
        "alice", ip="192.0.2.10", now=0.0, mailer=broken_mailer
    )
    assert result["status"] == "mail_failed"
    assert gate.pending_tokens("alice") == 0


def test_tampered_token_is_rejected():
    gate = setup_gate()
    _, box = send_reset(gate)
    token = box["token"]
    replacement = "A" if token[-1] != "A" else "B"
    tampered = token[:-1] + replacement
    with pytest.raises(ResetTokenError) as exc:
        gate.reset(tampered, "new-password", now=10.0)
    assert exc.value.code == "invalid"


def test_existing_and_missing_accounts_get_identical_responses():
    gate = setup_gate()
    existing, _ = send_reset(gate)
    calls = []
    missing = gate.request_reset(
        "nobody", ip="192.0.2.10", now=0.0,
        mailer=lambda email, token: calls.append(token),
    )
    assert existing == missing
    assert existing["status"] == "accepted"
    assert calls == []


def test_reset_token_expires_after_thirty_minutes():
    gate = setup_gate()
    _, box = send_reset(gate, now=0.0)
    with pytest.raises(ResetTokenError) as exc:
        gate.reset(box["token"], "new-password", now=1801.0)
    assert exc.value.code == "expired"


def test_new_request_invalidates_the_previous_link():
    gate = setup_gate()
    _, first = send_reset(gate, now=0.0)
    _, second = send_reset(gate, now=10.0)
    with pytest.raises(ResetTokenError) as exc:
        gate.reset(first["token"], "new-password", now=20.0)
    assert exc.value.code == "superseded"
    assert gate.reset(second["token"], "new-password", now=21.0)["status"] == "ok"


def test_rate_limit_is_per_account_not_per_ip():
    gate = setup_gate()
    for index in range(3):
        send_reset(gate, now=0.0, ip="192.0.2.%d" % index)
    with pytest.raises(ResetTokenError) as exc:
        send_reset(gate, now=10.0, ip="198.51.100.99")
    assert exc.value.code == "rate_limited"


def test_deleted_account_cannot_use_an_issued_link():
    gate = setup_gate()
    _, box = send_reset(gate)
    gate.delete_account("alice")
    with pytest.raises(ResetTokenError) as exc:
        gate.reset(box["token"], "new-password", now=10.0)
    assert exc.value.code == "invalid"


def test_snapshot_does_not_contain_plaintext_token():
    gate = setup_gate()
    _, box = send_reset(gate)
    text = json.dumps(gate.snapshot(), default=str)
    assert box["token"] not in text


def test_admin_reset_requires_explicit_confirmation():
    gate = setup_gate()
    gate.add_session("alice")
    with pytest.raises(ResetTokenError) as exc:
        gate.admin_reset("root", "alice", now=10.0, ip="10.0.0.1")
    assert exc.value.code == "confirmation_required"
    result = gate.admin_reset(
        "root", "alice", confirm=True, now=11.0, ip="10.0.0.1"
    )
    assert result["status"] == "ok"
    assert gate.active_sessions("alice") == 0


def test_admin_reset_is_audited_with_actor():
    gate = setup_gate()
    gate.admin_reset("root", "alice", confirm=True, now=10.0, ip="10.0.0.1")
    events = gate.audit_events()
    assert events
    event = events[-1]
    assert event["actor"] == "root"
    assert event["action"] == "admin_reset"
    assert event["ip"] == "10.0.0.1"


def test_success_audit_records_ip_and_revoked_session_count():
    gate = setup_gate()
    gate.add_session("alice")
    gate.add_session("alice")
    _, box = send_reset(gate)
    gate.reset(box["token"], "new-password", now=10.0, ip="203.0.113.5")
    event = gate.audit_events()[-1]
    assert event["ip"] == "203.0.113.5"
    assert event["revoked_sessions"] == 2
    assert event["status"] == "ok"


def test_unknown_token_is_rejected():
    gate = setup_gate()
    with pytest.raises(ResetTokenError) as exc:
        gate.reset("not-a-real-token", "new-password", now=10.0)
    assert exc.value.code == "invalid"


def test_used_token_stays_used_after_restart():
    gate = setup_gate()
    _, box = send_reset(gate)
    gate.reset(box["token"], "new-password", now=10.0)
    restored = ResetTokenGate()
    restored.restore(gate.snapshot())
    with pytest.raises(ResetTokenError) as exc:
        restored.reset(box["token"], "new-password", now=20.0)
    assert exc.value.code == "reused"
