import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from smtp_starttls_policy import SmtpPolicyError, StartTlsPolicy
import smtp_starttls_policy


def test_required_domain_without_starttls_is_deferred():
    gate = StartTlsPolicy()
    gate.set_domain_policy("bank.example", "require")
    decision = gate.decide("bank.example", starttls=False)
    assert decision["action"] == "defer"
    assert decision["reason"] == "TLS_REQUIRED"
    assert decision["plaintext"] is False


def test_optional_domain_without_starttls_may_deliver_plaintext():
    gate = StartTlsPolicy()
    gate.set_domain_policy("news.example", "optional")
    decision = gate.decide("news.example", starttls=False)
    assert decision["action"] == "deliver"
    assert decision["plaintext"] is True


def test_tlsa_lookup_failure_is_fail_closed():
    gate = StartTlsPolicy()
    gate.set_domain_policy("secure.example", "require")
    decision = gate.decide("secure.example", starttls=True, tlsa_status="timeout")
    assert decision["action"] == "defer"
    assert decision["reason"] == "TLSA_FAILED"
    assert decision["plaintext"] is False


def test_certificate_hostname_must_match_mx():
    gate = StartTlsPolicy()
    gate.set_domain_policy("secure.example", "require")
    decision = gate.decide(
        "secure.example",
        starttls=True,
        tlsa_status="valid",
        cert_hostname="mx.attacker.example",
        mx_host="mx.secure.example",
    )
    assert decision["action"] == "defer"
    assert decision["reason"] == "CERT_HOSTNAME_MISMATCH"


def test_policy_update_increments_version_and_applies_immediately():
    gate = StartTlsPolicy()
    first = gate.set_domain_policy("example.com", "optional")
    assert gate.get_policy("example.com")["version"] == first
    second = gate.set_domain_policy("example.com", "require")
    policy = gate.get_policy("example.com")
    assert second == first + 1
    assert policy["mode"] == "require"
    assert policy["version"] == second


def test_concurrent_policy_lookup_hits_loader_once():
    gate = StartTlsPolicy()
    calls = []
    lock = threading.Lock()

    def loader(domain):
        with lock:
            calls.append(domain)
        time.sleep(0.02)
        return "require"

    with ThreadPoolExecutor(max_workers=16) as pool:
        results = list(pool.map(lambda _: gate.get_policy("burst.example", loader), range(32)))
    assert calls == ["burst.example"]
    assert len({item["version"] for item in results}) == 1
    assert all(item["mode"] == "require" for item in results)


def test_hot_update_closes_old_connection_before_next_message():
    gate = StartTlsPolicy()
    gate.set_domain_policy("example.com", "optional")
    old = gate.open_connection("example.com")
    gate.set_domain_policy("example.com", "require")
    new = gate.open_connection("example.com")
    assert old["policy_version"] < new["policy_version"]
    with pytest.raises(SmtpPolicyError) as exc:
        gate.send_message(old["id"], "q-1")
    assert exc.value.code == "RECONNECT_REQUIRED"
    assert gate.send_message(new["id"], "q-1") is True


def test_multi_mx_batch_uses_a_tls_capable_mx():
    gate = StartTlsPolicy()
    gate.set_domain_policy("example.com", "require")
    plan = gate.plan_delivery(
        "example.com",
        [
            {"host": "mx1.example", "starttls": False, "tlsa": "none"},
            {"host": "mx2.example", "starttls": True, "tlsa": "valid"},
        ],
    )
    assert plan["action"] == "deliver"
    assert plan["host"] == "mx2.example"
    assert plan["plaintext"] is False


def test_multi_mx_without_capable_host_is_deferred():
    gate = StartTlsPolicy()
    gate.set_domain_policy("example.com", "require")
    plan = gate.plan_delivery(
        "example.com",
        [
            {"host": "mx1.example", "starttls": False, "tlsa": "none"},
            {"host": "mx2.example", "starttls": False, "tlsa": "none"},
        ],
    )
    assert plan["action"] == "defer"
    assert plan["reason"] == "NO_TLS_CAPABLE_MX"


def test_retry_queue_stops_at_max_retries():
    gate = StartTlsPolicy(max_retries=5)
    gate.set_domain_policy("example.com", "require")
    gate.queue_message("q-1", "example.com", b"body")
    for _ in range(5):
        assert gate.retry("q-1")["status"] == "retry"
    final = gate.retry("q-1")
    assert final["status"] == "bounced"
    assert final["attempts"] == 5


def test_temporary_remote_error_is_not_a_bounce():
    gate = StartTlsPolicy()
    gate.set_domain_policy("example.com", "optional")
    temporary = gate.decide("example.com", starttls=True, remote_code=421)
    permanent = gate.decide("example.com", starttls=True, remote_code=550)
    assert temporary["action"] == "retry"
    assert temporary["reason"] == "TEMP_FAILURE"
    assert permanent["action"] == "bounce"
    assert permanent["reason"] == "PERM_FAILURE"


def test_downgrade_does_not_truncate_eight_bit_body():
    gate = StartTlsPolicy()
    gate.set_domain_policy("example.com", "optional")
    payload = bytes(range(128, 256)) * 8
    gate.queue_message("q-1", "example.com", payload)
    delivered = gate.prepare_payload("q-1", downgrade=True)
    assert delivered == payload
    assert len(delivered) == len(payload)


def test_audit_records_policy_decision_fields():
    gate = StartTlsPolicy()
    gate.set_domain_policy("example.com", "require")
    gate.decide("example.com", starttls=False, queue_id="q-1")
    events = gate.audit_events()
    assert len(events) == 1
    event = events[0]
    assert event["queue_id"] == "q-1"
    assert event["domain"] == "example.com"
    assert event["action"] == "defer"
    assert event["reason"] == "TLS_REQUIRED"
    assert event["policy_version"] == 1


def test_restart_queue_keeps_original_policy_context():
    gate = StartTlsPolicy()
    gate.set_domain_policy("example.com", "optional")
    gate.queue_message("q-1", "example.com", b"body")
    snapshot = gate.snapshot()
    restored = StartTlsPolicy()
    restored.restore(snapshot)
    assert restored.message_status("q-1")["policy_version"] == 1
    restored.set_domain_policy("example.com", "require")
    decision = restored.decide_for_queued("q-1")
    assert decision["action"] == "deliver"
    assert decision["plaintext"] is True


def test_valid_tlsa_and_hostname_deliver_encrypted():
    gate = StartTlsPolicy()
    gate.set_domain_policy("secure.example", "require")
    decision = gate.decide(
        "secure.example",
        starttls=True,
        tlsa_status="valid",
        cert_hostname="mx.secure.example",
        mx_host="mx.secure.example",
    )
    assert decision["action"] == "deliver"
    assert decision["plaintext"] is False
