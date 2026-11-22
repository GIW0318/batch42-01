import threading

import pytest

import est_enrollment_warden
from est_enrollment_warden import EstEnrollmentWarden, EstError

CA = {"subject": "CN=CA", "issuer": "CN=Root", "signature_valid": True,
      "ca": True}


def _warden(**kwargs):
    options = dict(ca_cert=CA, trust_store=("CN=Root",),
                   otps={"device-1": "otp-1", "device-2": "otp-2"},
                   quota_per_device=3)
    options.update(kwargs)
    return EstEnrollmentWarden(**options)


def _csr(subject="CN=device-1", public_key="pub-1", password="otp-1",
         key_usage=None):
    return {
        "subject": subject,
        "public_key": public_key,
        "challenge_password": password,
        "key_usage": list(key_usage or ["digitalSignature"]),
    }


def _client(public_key="pub-1"):
    return {"subject": "CN=device-1", "public_key": public_key}


def test_forged_cacerts_chain_rejected():
    warden = _warden()
    forged = [{"subject": "CN=CA", "issuer": "CN=Root",
               "signature_valid": False, "ca": True}]
    with pytest.raises(EstError) as exc:
        warden.verify_cacerts(forged)
    assert exc.value.code == "untrusted-chain"
    assert warden.verify_cacerts([CA]) is True


def test_challenge_password_mismatch_rejected():
    warden = _warden()
    with pytest.raises(EstError) as exc:
        warden.enroll("device-1", _csr(password="wrong"), "wrong",
                      client_cert=_client())
    assert exc.value.code == "challenge-mismatch"
    assert warden.issue_count("device-1") == 0


def test_client_cert_required():
    warden = _warden()
    with pytest.raises(EstError) as exc:
        warden.enroll("device-1", _csr(), "otp-1", client_cert=None)
    assert exc.value.code == "client-cert-required"


def test_quota_per_device_not_source_ip():
    warden = _warden(quota_per_device=1)
    warden.enroll("device-1", _csr(), "otp-1", client_cert=_client(),
                  source_ip="198.51.100.1")
    with pytest.raises(EstError) as exc:
        warden.enroll("device-1", _csr(), "otp-1", client_cert=_client(),
                      source_ip="203.0.113.9")
    assert exc.value.code == "quota-exceeded"


def test_concurrent_enroll_single_certificate():
    warden = _warden()
    barrier = threading.Barrier(2)
    serials = []
    errors = []
    lock = threading.Lock()

    def worker():
        barrier.wait()
        try:
            result = warden.enroll("device-1", _csr(), "otp-1",
                                   client_cert=_client())
            with lock:
                serials.append(result["serial"])
        except Exception as exc:
            with lock:
                errors.append(getattr(exc, "code", type(exc).__name__))

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(set(serials)) == 1
    assert warden.issue_count("device-1") == 1
    assert errors == []


def test_duplicate_enroll_returns_same_serial():
    warden = _warden()
    first = warden.enroll("device-1", _csr(), "otp-1", client_cert=_client())
    second = warden.enroll("device-1", _csr(), "otp-1", client_cert=_client())
    assert first["serial"] == second["serial"]
    assert warden.issue_count("device-1") == 1


def test_pending_202_retry_issues_certificate():
    warden = _warden()
    result = warden.enroll("device-1", _csr(), "otp-1", client_cert=_client(),
                           defer=True, now=0)
    assert result["status"] == "pending"
    assert result["retry_after"] == 5
    assert warden.task_state("device-1") == "pending"
    assert warden.pending_count() == 1
    issued = warden.retry_pending(now=5)
    assert len(issued) == 1
    assert issued[0]["serial"]
    assert warden.pending_count() == 0


def test_pkcs7_untrusted_signer_rejected():
    warden = _warden()
    with pytest.raises(EstError) as exc:
        warden.verify_response({"signer": "CN=Evil", "signature_valid": True})
    assert exc.value.code == "untrusted-signer"
    assert warden.verify_response({"signer": "CN=CA",
                                   "signature_valid": True}) is True


def test_key_usage_restricted():
    warden = _warden()
    cert = warden.enroll(
        "device-1",
        _csr(key_usage=["digitalSignature", "keyCertSign"]),
        "otp-1",
        client_cert=_client(),
    )
    assert cert["key_usage"] == ["digitalSignature"]
    assert "keyCertSign" not in cert["key_usage"]


def test_renewal_subject_mismatch():
    warden = _warden()
    cert = warden.enroll("device-1", _csr(), "otp-1", client_cert=_client())
    with pytest.raises(EstError) as exc:
        warden.renew("device-1", cert, _csr(subject="CN=device-9"),
                     client_cert=_client())
    assert exc.value.code == "subject-mismatch"


def test_renewal_key_mismatch():
    warden = _warden()
    cert = warden.enroll("device-1", _csr(), "otp-1", client_cert=_client())
    with pytest.raises(EstError) as exc:
        warden.renew("device-1", cert, _csr(public_key="pub-9"),
                     client_cert=_client())
    assert exc.value.code == "key-mismatch"


def test_revoked_device_cannot_renew():
    warden = _warden()
    cert = warden.enroll("device-1", _csr(), "otp-1", client_cert=_client())
    warden.revoke(cert["serial"])
    with pytest.raises(EstError) as exc:
        warden.renew("device-1", cert, _csr(), client_cert=_client())
    assert exc.value.code == "revoked"


def test_restart_restores_pending_tasks():
    warden = _warden()
    warden.enroll("device-1", _csr(), "otp-1", client_cert=_client(),
                  defer=True, now=0)
    snapshot = warden.snapshot()
    restored = _warden()
    restored.restore(snapshot)
    assert restored.pending_count() == 1
    assert restored.retry_pending(now=5)[0]["serial"]


def test_issued_certificate_matches_csr():
    warden = _warden()
    cert = warden.enroll("device-1", _csr(), "otp-1", client_cert=_client())
    assert cert["subject"] == "CN=device-1"
    assert cert["public_key"] == "pub-1"
    assert cert["serial"]


def test_invalid_csr_missing_subject():
    warden = _warden()
    with pytest.raises(EstError) as exc:
        warden.enroll("device-1", {"public_key": "pub-1"}, "otp-1",
                      client_cert=_client())
    assert exc.value.code == "invalid-csr"
