import threading

import radius_acct_ledger as ral


SECRETS = {"nas1": "shared-secret"}


def new_ledger(**kwargs):
    return ral.RadiusAcctLedger(secrets=dict(SECRETS), **kwargs)


def packet(
    ledger,
    session_id="S-1",
    user="alice",
    status="Start",
    octets=0,
    session_time=100,
    nas="nas1",
    signer=None,
):
    signer = signer or ledger
    return {
        "nas": nas,
        "session_id": session_id,
        "user": user,
        "status": status,
        "octets": octets,
        "session_time": session_time,
        "authenticator": signer.make_authenticator(
            nas, session_id, status, octets, session_time
        ),
    }


def test_start_creates_single_open_session():
    ledger = new_ledger()
    result = ledger.process(packet(ledger))
    assert result["code"] == "Accounting-Response"
    assert result["status"] == "started"
    assert result["created"] is True
    assert result["billed"] is False
    assert ledger.session_count() == 1
    assert ledger.session_usage("nas1", "S-1") == 0
    assert len(ledger.open_sessions()) == 1


def test_duplicate_start_is_idempotent():
    ledger = new_ledger()
    first = ledger.process(packet(ledger))
    second = ledger.process(packet(ledger))
    assert first["status"] == "started"
    assert second["status"] == "duplicate_start"
    assert second["created"] is False
    assert ledger.session_count() == 1
    assert len(ledger.open_sessions()) == 1


def test_session_id_is_trimmed_and_case_folded():
    ledger = new_ledger()
    ledger.process(packet(ledger, session_id=" S-1 "))
    duplicate = ledger.process(packet(ledger, session_id="s-1"))
    assert duplicate["status"] == "duplicate_start"
    assert ledger.session_count() == 1
    assert ledger.session_usage("nas1", "s-1") == 0


def test_interim_cannot_regress_usage():
    ledger = new_ledger()
    ledger.process(packet(ledger))
    newer = ledger.process(
        packet(ledger, status="Interim-Update", octets=500), now=10.0
    )
    older = ledger.process(
        packet(ledger, status="Interim-Update", octets=300), now=20.0
    )
    assert newer["status"] == "updated"
    assert older["status"] == "stale_interim"
    assert ledger.session_usage("nas1", "S-1") == 500


def test_stop_before_interim_closes_and_bills():
    ledger = new_ledger()
    ledger.process(packet(ledger))
    result = ledger.process(
        packet(ledger, status="Stop", octets=700), now=10.0
    )
    assert result["status"] == "stopped"
    assert result["billed"] is True
    assert ledger.session_usage("nas1", "S-1") == 700
    assert ledger.billed_total("alice") == 700
    assert ledger.open_sessions() == []


def test_late_interim_after_stop_does_not_reopen_session():
    ledger = new_ledger()
    ledger.process(packet(ledger))
    ledger.process(packet(ledger, status="Stop", octets=700), now=10.0)
    late = ledger.process(
        packet(ledger, status="Interim-Update", octets=900), now=20.0
    )
    assert late["status"] == "late_interim"
    assert late["billed"] is False
    assert ledger.session_usage("nas1", "S-1") == 700
    assert ledger.open_sessions() == []
    assert ledger.billed_total("alice") == 700


def test_duplicate_stop_bills_only_once():
    ledger = new_ledger()
    ledger.process(packet(ledger))
    ledger.process(packet(ledger, status="Stop", octets=700), now=10.0)
    duplicate = ledger.process(
        packet(ledger, status="Stop", octets=700), now=11.0
    )
    assert duplicate["status"] == "duplicate_stop"
    assert duplicate["billed"] is False
    assert ledger.billed_total("alice") == 700


def test_bad_authenticator_is_dropped_without_writing():
    ledger = new_ledger()
    wrong = ral.RadiusAcctLedger(secrets={"nas1": "wrong-secret"})
    result = ledger.process(packet(ledger, status="Start", signer=wrong))
    assert result["status"] == "rejected"
    assert result["reason"] == "bad authenticator"
    assert result["created"] is False
    assert ledger.session_count() == 0
    assert ledger.open_sessions() == []
    assert ledger.billed_total("alice") == 0


def test_unknown_accounting_status_is_rejected():
    ledger = new_ledger()
    result = ledger.process(packet(ledger, status="Unknown"))
    assert result["status"] == "unknown_status"
    assert result["created"] is False
    assert ledger.session_count() == 0


def test_restart_restores_open_session_and_stop_bills():
    ledger = new_ledger()
    ledger.process(packet(ledger))
    ledger.process(
        packet(ledger, status="Interim-Update", octets=300), now=10.0
    )
    state = ledger.export_state()
    restarted = new_ledger()
    restarted.load_state(state)
    assert len(restarted.open_sessions()) == 1
    assert restarted.session_usage("nas1", "S-1") == 300
    result = restarted.process(
        packet(restarted, status="Stop", octets=400), now=20.0
    )
    assert result["status"] == "stopped"
    assert result["billed"] is True
    assert restarted.session_usage("nas1", "S-1") == 400
    assert restarted.billed_total("alice") == 400


def test_reused_session_id_after_stop_starts_new_session():
    ledger = new_ledger()
    ledger.process(packet(ledger, session_time=100))
    ledger.process(
        packet(ledger, status="Stop", octets=500, session_time=100), now=10.0
    )
    restarted = ledger.process(
        packet(ledger, session_time=200, octets=0), now=20.0
    )
    assert restarted["status"] == "started"
    assert restarted["created"] is True
    assert ledger.session_count() == 2
    ledger.process(
        packet(ledger, status="Stop", octets=300, session_time=200), now=30.0
    )
    assert ledger.billed_total("alice") == 800


def test_same_user_on_two_devices_keeps_sessions_separate():
    ledger = new_ledger()
    ledger.process(
        packet(ledger, session_id="S-phone", status="Start", session_time=1)
    )
    ledger.process(
        packet(ledger, session_id="S-laptop", status="Start", session_time=2)
    )
    ledger.process(
        packet(
            ledger,
            session_id="S-phone",
            status="Stop",
            octets=100,
            session_time=1,
        ),
        now=10.0,
    )
    ledger.process(
        packet(
            ledger,
            session_id="S-laptop",
            status="Stop",
            octets=150,
            session_time=2,
        ),
        now=11.0,
    )
    assert ledger.billed_total("alice") == 250
    assert ledger.session_count() == 2
    assert ledger.open_sessions() == []


def test_interim_coalescing_keeps_maximum_usage():
    ledger = new_ledger(min_interim_interval=5.0)
    ledger.process(packet(ledger))
    ledger.process(
        packet(ledger, status="Interim-Update", octets=100), now=10.0
    )
    coalesced = ledger.process(
        packet(ledger, status="Interim-Update", octets=200), now=12.0
    )
    assert coalesced["status"] == "coalesced"
    assert ledger.session_usage("nas1", "S-1") == 200
    updated = ledger.process(
        packet(ledger, status="Interim-Update", octets=300), now=20.0
    )
    assert updated["status"] == "updated"
    assert ledger.session_usage("nas1", "S-1") == 300


def test_concurrent_duplicate_start_writes_one_row():
    ledger = new_ledger()
    request = packet(ledger)
    barrier = threading.Barrier(2)
    results = []
    results_lock = threading.Lock()

    def submit():
        barrier.wait()
        result = ledger.process(request)
        with results_lock:
            results.append(result)

    threads = [threading.Thread(target=submit) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)

    assert sorted(item["status"] for item in results) == [
        "duplicate_start",
        "started",
    ]
    assert ledger.session_count() == 1
    assert len(ledger.open_sessions()) == 1


def test_restart_keeps_duplicate_stop_idempotent():
    ledger = new_ledger()
    ledger.process(packet(ledger))
    ledger.process(packet(ledger, status="Stop", octets=500), now=10.0)
    state = ledger.export_state()
    restarted = new_ledger()
    restarted.load_state(state)
    duplicate = restarted.process(
        packet(restarted, status="Stop", octets=500), now=20.0
    )
    assert duplicate["status"] == "duplicate_stop"
    assert duplicate["billed"] is False
    assert restarted.billed_total("alice") == 500
    assert restarted.session_count() == 1
