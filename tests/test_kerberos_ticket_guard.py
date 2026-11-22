import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from kerberos_ticket_guard import KerberosError, KerberosTicketGuard
import kerberos_ticket_guard


def ticket(**overrides):
    value = {
        "realm": "EXAMPLE.COM",
        "principal": "svc@EXAMPLE.COM",
        "service": "svc@EXAMPLE.COM",
        "kvno": 1,
        "ticket_id": "T-0001",
        "issue_time": 990.0,
        "expiry": 2000.0,
        "client_addrs": ["10.0.0.1"],
        "pac_signature_valid": True,
        "referral_chain": ["EXAMPLE.COM"],
        "delegated_to": None,
    }
    value.update(overrides)
    return value


def test_untrusted_realm_is_rejected():
    guard = KerberosTicketGuard()
    guard.add_realm("EXAMPLE.COM")
    with pytest.raises(KerberosError) as exc:
        guard.authenticate(
            ticket(realm="OTHER.COM"),
            "10.0.0.1",
            now=1000.0,
            service="svc@EXAMPLE.COM",
        )
    assert exc.value.code == "REALM_MISMATCH"


def test_principal_components_must_match_exactly():
    guard = KerberosTicketGuard()
    guard.add_realm("EXAMPLE.COM")
    with pytest.raises(KerberosError) as exc:
        guard.authenticate(
            ticket(principal="admin/service@EXAMPLE.COM", service="admin/service@EXAMPLE.COM"),
            "10.0.0.1",
            now=1000.0,
            service="admin@EXAMPLE.COM",
        )
    assert exc.value.code == "PRINCIPAL_MISMATCH"


def test_principal_comparison_is_case_sensitive():
    guard = KerberosTicketGuard()
    guard.add_realm("EXAMPLE.COM")
    with pytest.raises(KerberosError) as exc:
        guard.authenticate(
            ticket(principal="admin@EXAMPLE.COM", service="admin@EXAMPLE.COM"),
            "10.0.0.1",
            now=1000.0,
            service="ADMIN@EXAMPLE.COM",
        )
    assert exc.value.code == "PRINCIPAL_MISMATCH"


def test_old_kvno_is_accepted_only_during_rotation_overlap():
    guard = KerberosTicketGuard()
    guard.add_realm("EXAMPLE.COM", current_kvno=2)
    guard.rotate_keytab("svc@EXAMPLE.COM", 3, now=0.0, overlap=300.0)
    old = ticket(kvno=2, ticket_id="T-OLD", issue_time=0.0, expiry=5000.0)
    assert guard.authenticate(
        old, "10.0.0.1", now=100.0, service="svc@EXAMPLE.COM"
    )["kvno"] == 2
    expired_old = ticket(kvno=2, ticket_id="T-OLD-2", issue_time=0.0, expiry=5000.0)
    with pytest.raises(KerberosError) as exc:
        guard.authenticate(
            expired_old, "10.0.0.1", now=301.0, service="svc@EXAMPLE.COM"
        )
    assert exc.value.code == "KVNO_MISMATCH"


def test_same_ticket_and_address_can_only_be_used_once():
    guard = KerberosTicketGuard()
    guard.add_realm("EXAMPLE.COM")
    data = ticket()
    guard.authenticate(data, "10.0.0.1", now=1000.0, service="svc@EXAMPLE.COM")
    with pytest.raises(KerberosError) as exc:
        guard.authenticate(data, "10.0.0.1", now=1001.0, service="svc@EXAMPLE.COM")
    assert exc.value.code == "REPLAY"


def test_ticket_address_binding_is_enforced():
    guard = KerberosTicketGuard()
    guard.add_realm("EXAMPLE.COM")
    with pytest.raises(KerberosError) as exc:
        guard.authenticate(
            ticket(), "10.0.0.9", now=1000.0, service="svc@EXAMPLE.COM"
        )
    assert exc.value.code == "ADDR_RESTRICTED"


def test_clock_skew_bounds_are_enforced():
    guard = KerberosTicketGuard(clock_skew=300)
    guard.add_realm("EXAMPLE.COM")
    with pytest.raises(KerberosError) as exc:
        guard.authenticate(
            ticket(issue_time=1400.0), "10.0.0.1", now=1000.0,
            service="svc@EXAMPLE.COM"
        )
    assert exc.value.code == "CLOCK_SKEW"
    within = ticket(issue_time=1290.0, ticket_id="T-SKEW-OK")
    assert guard.authenticate(
        within, "10.0.0.1", now=1000.0, service="svc@EXAMPLE.COM"
    )["ticket_id"] == "T-SKEW-OK"


def test_expired_ticket_is_rejected():
    guard = KerberosTicketGuard(clock_skew=300)
    guard.add_realm("EXAMPLE.COM")
    with pytest.raises(KerberosError) as exc:
        guard.authenticate(
            ticket(expiry=600.0), "10.0.0.1", now=1000.0,
            service="svc@EXAMPLE.COM"
        )
    assert exc.value.code == "CLOCK_SKEW"


def test_invalid_pac_signature_is_rejected():
    guard = KerberosTicketGuard()
    guard.add_realm("EXAMPLE.COM")
    with pytest.raises(KerberosError) as exc:
        guard.authenticate(
            ticket(pac_signature_valid=False), "10.0.0.1", now=1000.0,
            service="svc@EXAMPLE.COM"
        )
    assert exc.value.code == "PAC_INVALID"


def test_empty_address_list_is_restricted():
    guard = KerberosTicketGuard()
    guard.add_realm("EXAMPLE.COM")
    with pytest.raises(KerberosError) as exc:
        guard.authenticate(
            ticket(client_addrs=[]), "10.0.0.1", now=1000.0,
            service="svc@EXAMPLE.COM"
        )
    assert exc.value.code == "ADDR_RESTRICTED"


def test_referral_chain_must_be_fully_trusted():
    guard = KerberosTicketGuard()
    guard.add_realm("EXAMPLE.COM")
    with pytest.raises(KerberosError) as exc:
        guard.authenticate(
            ticket(referral_chain=["EXAMPLE.COM", "EVIL.COM"]),
            "10.0.0.1",
            now=1000.0,
            service="svc@EXAMPLE.COM",
        )
    assert exc.value.code == "UNTRUSTED_REFERRAL"


def test_delegation_target_must_be_authorized():
    guard = KerberosTicketGuard()
    guard.add_realm("EXAMPLE.COM")
    with pytest.raises(KerberosError) as exc:
        guard.delegate(
            ticket(), "10.0.0.1", "ldap@EXAMPLE.COM", now=1000.0
        )
    assert exc.value.code == "DELEGATION_DENIED"


def test_restart_preserves_replay_cache():
    guard = KerberosTicketGuard()
    guard.add_realm("EXAMPLE.COM")
    data = ticket()
    guard.authenticate(data, "10.0.0.1", now=1000.0, service="svc@EXAMPLE.COM")
    saved = guard.snapshot()
    restored = KerberosTicketGuard()
    restored.restore(saved)
    with pytest.raises(KerberosError) as exc:
        restored.authenticate(
            data, "10.0.0.1", now=1001.0, service="svc@EXAMPLE.COM"
        )
    assert exc.value.code == "REPLAY"


def test_concurrent_same_ticket_succeeds_once():
    guard = KerberosTicketGuard()
    guard.add_realm("EXAMPLE.COM")
    data = ticket()
    barrier = threading.Barrier(8)

    def worker(_):
        barrier.wait()
        try:
            guard.authenticate(
                data, "10.0.0.1", now=1000.0, service="svc@EXAMPLE.COM"
            )
        except KerberosError as exc:
            return exc.code
        return "ok"

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(worker, range(8)))
    assert results.count("ok") == 1
    assert results.count("REPLAY") == 7


def test_audit_contains_realm_kvno_and_ticket_id():
    guard = KerberosTicketGuard()
    guard.add_realm("EXAMPLE.COM")
    guard.authenticate(
        ticket(), "10.0.0.1", now=1000.0, service="svc@EXAMPLE.COM"
    )
    events = guard.audit_events()
    assert events
    event = events[-1]
    assert event["realm"] == "EXAMPLE.COM"
    assert event["kvno"] == 1
    assert event["ticket_id"] == "T-0001"
