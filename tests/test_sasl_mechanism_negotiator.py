import threading

import pytest

import sasl_mechanism_negotiator
from sasl_mechanism_negotiator import SaslError, SaslMechanismNegotiator


def _negotiator(**kwargs):
    return SaslMechanismNegotiator(**kwargs)


def test_strongest_mechanism_chosen_regardless_of_order():
    neg = _negotiator()
    weak_first = neg.negotiate("c1", ["PLAIN", "SCRAM-SHA-256"])
    assert weak_first["mechanism"] == "SCRAM-SHA-256"
    strong_first = neg.negotiate("c2", ["SCRAM-SHA-256", "GSSAPI"])
    assert strong_first["mechanism"] == "GSSAPI"


def test_mechanism_names_case_insensitive():
    neg = _negotiator()
    result = neg.negotiate("c1", ["scram-sha-256", "plain"])
    assert result["mechanism"] == "SCRAM-SHA-256"


def test_no_common_mechanism_rejected():
    neg = _negotiator(mechanisms=("SCRAM-SHA-256",))
    with pytest.raises(SaslError) as exc:
        neg.negotiate("c1", ["PLAIN"])
    assert exc.value.code == "no-mechanism"


def test_unknown_mechanism_rejected():
    neg = _negotiator()
    with pytest.raises(SaslError) as exc:
        neg.negotiate("c1", ["PLAIN", "MAGIC"])
    assert exc.value.code == "unknown-mechanism"
    assert neg.connection_state("c1") == "none"


def test_external_requires_client_certificate():
    neg = _negotiator()
    with pytest.raises(SaslError) as exc:
        neg.negotiate("c1", ["EXTERNAL"], has_client_cert=False)
    assert exc.value.code == "certificate-required"
    assert neg.negotiate("c1", ["EXTERNAL"], has_client_cert=True)["mechanism"] == "EXTERNAL"


def test_external_requires_explicit_authzid():
    neg = _negotiator()
    neg.negotiate("c1", ["EXTERNAL"], has_client_cert=True)
    with pytest.raises(SaslError) as exc:
        neg.start("c1", "EXTERNAL", b"", authcid="alice", authzid="",
                  has_client_cert=True)
    assert exc.value.code == "invalid-authzid"
    assert neg.start("c1", "EXTERNAL", b"", authcid="alice", authzid="alice",
                     has_client_cert=True)["authzid"] == "alice"


def test_initial_response_too_large():
    neg = _negotiator(max_initial=8)
    neg.negotiate("c1", ["PLAIN"])
    with pytest.raises(SaslError) as exc:
        neg.start("c1", "PLAIN", b"x" * 9, authcid="alice")
    assert exc.value.code == "response-too-large"


def test_concurrent_step_serialized():
    neg = _negotiator()
    neg.negotiate("c1", ["SCRAM-SHA-256"])
    neg.start("c1", "SCRAM-SHA-256", b"client-first", authcid="alice")
    barrier = threading.Barrier(2)
    results = []
    errors = []
    lock = threading.Lock()

    def worker():
        barrier.wait()
        try:
            outcome = neg.step("c1", b"client-final")
            with lock:
                results.append(outcome["state"])
        except Exception as exc:
            with lock:
                errors.append(getattr(exc, "code", type(exc).__name__))

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(results) + len(errors) == 2
    assert neg.connection_state("c1") in ("authenticated", "in-progress")


def test_abort_clears_session():
    neg = _negotiator()
    neg.negotiate("c1", ["SCRAM-SHA-256"])
    neg.start("c1", "SCRAM-SHA-256", b"client-first", authcid="alice")
    neg.abort("c1")
    assert neg.connection_state("c1") == "aborted"
    with pytest.raises(SaslError) as exc:
        neg.step("c1", b"client-final")
    assert exc.value.code == "no-session"


def test_authzid_proxy_restricted():
    neg = _negotiator()
    neg.negotiate("c1", ["PLAIN"])
    neg.start("c1", "PLAIN", b"payload", authcid="alice")
    with pytest.raises(SaslError) as exc:
        neg.bind("c1", "alice", authzid="bob")
    assert exc.value.code == "authzid-not-allowed"


def test_authzid_proxy_allowed():
    neg = _negotiator(proxy_allowed=("alice",))
    neg.negotiate("c1", ["PLAIN"])
    neg.start("c1", "PLAIN", b"payload", authcid="alice")
    result = neg.bind("c1", "alice", authzid="bob")
    assert result["authcid"] == "alice"
    assert result["authzid"] == "bob"


def test_failure_count_per_account_across_connections():
    neg = _negotiator(max_failures=3)
    for _ in range(3):
        neg.record_failure("alice")
    assert neg.attempt_allowed("alice") is False
    assert neg.attempt_allowed("bob") is True
    neg.negotiate("new-connection", ["PLAIN"])
    with pytest.raises(SaslError) as exc:
        neg.start("new-connection", "PLAIN", b"payload", authcid="alice")
    assert exc.value.code == "account-locked"


def test_duplicate_bind_idempotent():
    neg = _negotiator()
    neg.negotiate("c1", ["PLAIN"])
    neg.start("c1", "PLAIN", b"payload", authcid="alice")
    first = neg.bind("c1", "alice")
    second = neg.bind("c1", "alice")
    assert first["authcid"] == second["authcid"] == "alice"
    assert second["duplicate"] is True


def test_gssapi_security_layer_none_not_encrypted():
    neg = _negotiator()
    neg.negotiate("c1", ["GSSAPI"])
    neg.start("c1", "GSSAPI", b"token", authcid="alice")
    result = neg.step("c1", b"token-response")
    assert result["security_layer"] == "none"
    assert neg.security_layer("c1") == "none"
    assert neg.claims_encrypted("c1") is False


def test_channel_binding_required_rejects_and_logs():
    neg = _negotiator(require_channel_binding=True)
    with pytest.raises(SaslError) as exc:
        neg.negotiate("c1", ["SCRAM-SHA-256"], channel_binding=False)
    assert exc.value.code == "channel-binding-required"
    entries = [item for item in neg.audit_log() if item["kind"] == "downgrade"]
    assert len(entries) == 1
