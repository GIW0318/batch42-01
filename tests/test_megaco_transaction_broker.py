import threading

import pytest

import megaco_transaction_broker
from megaco_transaction_broker import MegacoError, MegacoTransactionBroker


def _broker(**kwargs):
    broker = MegacoTransactionBroker(**kwargs)
    broker.create_context("ctx1")
    return broker


def test_termination_id_normalized():
    broker = _broker()
    assert broker.normalize_termination_id("Term/0001") == "term/1"
    assert broker.normalize_termination_id("TERM/1") == "term/1"


def test_duplicate_termination_idempotent():
    broker = _broker()
    broker.add_termination("ctx1", "Term/0001")
    broker.add_termination("ctx1", "term/1")
    assert broker.termination_count("ctx1") == 1
    assert broker.audit_value("ctx1") == ["term/1"]


def test_duplicate_transaction_cached_without_reexecution():
    broker = _broker()
    broker.add_termination("ctx1", "term/1")
    first = broker.transaction("tx1", [{"action": "play", "context": "ctx1",
                                        "termination": "term/1"}])
    second = broker.transaction("tx1", [{"action": "play", "context": "ctx1",
                                         "termination": "term/1"}])
    assert second["duplicate"] is True
    assert broker.play_count("term/1") == 1


def test_action_failure_rolls_back_all():
    broker = _broker()
    actions = [
        {"action": "add", "context": "ctx1", "termination": "term/1"},
        {"action": "add", "context": "missing", "termination": "term/2"},
        {"action": "add", "context": "ctx1", "termination": "term/3"},
    ]
    with pytest.raises(MegacoError) as exc:
        broker.transaction("tx1", actions)
    assert exc.value.code == "unknown-context"
    assert broker.termination_count("ctx1") == 0


def test_context_and_add_concurrent():
    broker = MegacoTransactionBroker()
    barrier = threading.Barrier(2)
    errors = []

    def worker(context_id, term):
        barrier.wait()
        try:
            broker.create_context(context_id)
            broker.add_termination(context_id, term)
        except Exception as exc:
            errors.append(getattr(exc, "code", type(exc).__name__))

    threads = [
        threading.Thread(target=worker, args=("ctx-a", "term-a")),
        threading.Thread(target=worker, args=("ctx-b", "term-b")),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    assert broker.termination_count("ctx-a") == 1
    assert broker.termination_count("ctx-b") == 1


def test_audit_value_not_truncated():
    broker = _broker(max_terminations=100)
    for number in range(60):
        broker.add_termination("ctx1", "term/%d" % number)
    assert len(broker.audit_value("ctx1")) == 60


def test_keepalive_answered():
    broker = _broker()
    reply = broker.keepalive("ctx1")
    assert reply["keepalive"] is True
    assert reply["context"] == "ctx1"


def test_empty_context_swept():
    broker = _broker()
    broker.create_context("ctx-empty")
    broker.add_termination("ctx1", "term/1")
    removed = broker.sweep_empty()
    assert removed == ["ctx-empty"]
    assert broker.context_count() == 1


def test_emergency_flag_preserved():
    broker = _broker()
    broker.transaction("tx1", [{"action": "add", "context": "ctx1",
                                "termination": "term/9", "emergency": True}])
    assert broker.termination_info("ctx1", "term/9")["emergency"] is True


def test_decode_header_mismatch():
    broker = _broker()
    with pytest.raises(MegacoError) as exc:
        broker.decode_transaction(2, "text-encoded")
    assert exc.value.code == "encoding-mismatch"
    with pytest.raises(MegacoError) as exc:
        broker.decode_transaction(1, b"binary-encoded")
    assert exc.value.code == "encoding-mismatch"


def test_tx_busy_while_pending():
    broker = _broker()
    broker.begin_transaction("tx1")
    with pytest.raises(MegacoError) as exc:
        broker.transaction("tx1", [{"action": "add", "context": "ctx1",
                                    "termination": "term/1"}])
    assert exc.value.code == "tx-busy"
    broker.end_transaction("tx1")
    result = broker.transaction("tx1", [{"action": "add", "context": "ctx1",
                                         "termination": "term/1"}])
    assert result["results"][0]["termination"] == "term/1"


def test_restart_reregisters_inflight():
    broker = _broker()
    broker.begin_transaction("tx1")
    snapshot = broker.snapshot()
    restored = MegacoTransactionBroker()
    restored.restore(snapshot)
    assert restored.re_register() == ["tx1"]
    restored.end_transaction("tx1")
    assert restored.transaction("tx1", [{"action": "add", "context": "ctx1",
                                         "termination": "term/1"}])


def test_max_terminations_atomic():
    broker = _broker(max_terminations=1)
    broker.add_termination("ctx1", "term/1")
    with pytest.raises(MegacoError) as exc:
        broker.add_termination("ctx1", "term/2")
    assert exc.value.code == "termination-limit"
    assert broker.termination_count("ctx1") == 1


def test_delete_context_releases_terminations():
    broker = _broker()
    broker.add_termination("ctx1", "term/1")
    broker.add_termination("ctx1", "term/2")
    assert broker.delete_context("ctx1") is True
    assert broker.context_count() == 0
    with pytest.raises(MegacoError) as exc:
        broker.add_termination("ctx1", "term/3")
    assert exc.value.code == "unknown-context"


def test_unknown_context_rejected():
    broker = _broker()
    with pytest.raises(MegacoError) as exc:
        broker.audit_value("missing")
    assert exc.value.code == "unknown-context"
    with pytest.raises(MegacoError) as exc:
        broker.transaction("tx1", [{"action": "add", "context": "missing",
                                    "termination": "term/1"}])
    assert exc.value.code == "unknown-context"
