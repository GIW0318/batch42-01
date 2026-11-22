import threading

import pytest

import openid4vp_presentation_mapper
from openid4vp_presentation_mapper import (
    INVALID_PRESENTATION,
    Openid4vpPresentationMapper,
    VpError,
)

AUDIENCE = "verifier.example"


def _mapper(**kwargs):
    return Openid4vpPresentationMapper(audience=AUDIENCE, **kwargs)


def _request(mapper, descriptors=None):
    descriptors = descriptors or [{"id": "dl", "format": "sd-jwt"}]
    return mapper.create_request("session-1", descriptors)


def _presentation(request, **overrides):
    presentation = {
        "format": "sd-jwt",
        "descriptor_map": [{"id": "dl", "credential": "cred-1"}],
        "credentials": {
            "cred-1": {
                "format": "sd-jwt",
                "valid_from": 10,
                "valid_until": 100,
                "claims": {"given_name": "Ada", "family_name": "Lovelace"},
                "disclosure_digests_valid": True,
                "revoked": False,
            }
        },
        "kb_jwt": {"nonce": request["nonce"], "aud": AUDIENCE, "valid": True},
        "encrypted": True,
    }
    presentation.update(overrides)
    return presentation


def test_extra_descriptor_rejected():
    mapper = _mapper()
    request = _request(mapper)
    presentation = _presentation(request, descriptor_map=[
        {"id": "dl", "credential": "cred-1"},
        {"id": "extra", "credential": "cred-2"},
    ])
    with pytest.raises(VpError):
        mapper.submit("session-1", request, presentation, now=20)


def test_missing_descriptor_rejected():
    mapper = _mapper()
    request = _request(mapper, [
        {"id": "dl", "format": "sd-jwt"},
        {"id": "age", "format": "sd-jwt"},
    ])
    presentation = _presentation(request)
    with pytest.raises(VpError):
        mapper.submit("session-1", request, presentation, now=20)


def test_duplicate_credential_reference_rejected():
    mapper = _mapper()
    request = _request(mapper, [
        {"id": "dl", "format": "sd-jwt"},
        {"id": "age", "format": "sd-jwt"},
    ])
    presentation = _presentation(request, descriptor_map=[
        {"id": "dl", "credential": "cred-1"},
        {"id": "age", "credential": "cred-1"},
    ])
    with pytest.raises(VpError):
        mapper.submit("session-1", request, presentation, now=20)


def test_kb_jwt_nonce_mismatch_rejected():
    mapper = _mapper()
    request = _request(mapper)
    presentation = _presentation(request, kb_jwt={
        "nonce": "other-nonce", "aud": AUDIENCE, "valid": True,
    })
    with pytest.raises(VpError):
        mapper.submit("session-1", request, presentation, now=20)


def test_nonce_single_use():
    mapper = _mapper()
    request = _request(mapper)
    presentation = _presentation(request)
    assert mapper.submit("session-1", request, presentation, now=20)["subject"]
    with pytest.raises(VpError):
        mapper.submit("session-1", request, presentation, now=20)


def test_audience_mismatch_rejected():
    mapper = _mapper()
    request = _request(mapper)
    presentation = _presentation(request, kb_jwt={
        "nonce": request["nonce"], "aud": "other-app", "valid": True,
    })
    with pytest.raises(VpError):
        mapper.submit("session-1", request, presentation, now=20)


def test_format_mismatch_rejected():
    mapper = _mapper()
    request = _request(mapper)
    presentation = _presentation(request, format="mdoc")
    with pytest.raises(VpError):
        mapper.submit("session-1", request, presentation, now=20)


def test_expired_boundary_rejected():
    mapper = _mapper()
    request = _request(mapper)
    presentation = _presentation(request)
    with pytest.raises(VpError):
        mapper.submit("session-1", request, presentation, now=100)


def test_valid_from_boundary_inclusive():
    mapper = _mapper()
    request = _request(mapper)
    presentation = _presentation(request)
    result = mapper.submit("session-1", request, presentation, now=10)
    assert result["subject"] == "cred-1"


def test_disclosure_digest_mismatch_rejected():
    mapper = _mapper()
    request = _request(mapper)
    presentation = _presentation(request)
    presentation["credentials"]["cred-1"]["disclosure_digests_valid"] = False
    with pytest.raises(VpError):
        mapper.submit("session-1", request, presentation, now=20)


def test_revoked_credential_rejected():
    mapper = _mapper(revoked={"cred-1"})
    request = _request(mapper)
    presentation = _presentation(request)
    with pytest.raises(VpError):
        mapper.submit("session-1", request, presentation, now=20)


def test_revocation_unavailable_denies():
    mapper = _mapper(revocation_available=False)
    request = _request(mapper)
    presentation = _presentation(request)
    with pytest.raises(VpError):
        mapper.submit("session-1", request, presentation, now=20)


def test_direct_post_must_be_encrypted():
    mapper = _mapper()
    request = _request(mapper)
    presentation = _presentation(request, encrypted=False)
    with pytest.raises(VpError):
        mapper.submit("session-1", request, presentation, now=20)


def test_uniform_error_code_and_message():
    mapper = _mapper()
    request = _request(mapper)
    bad_audience = _presentation(request, kb_jwt={
        "nonce": request["nonce"], "aud": "other-app", "valid": True,
    })
    expired = _presentation(request)
    codes = []
    messages = []
    for presentation, now in [(bad_audience, 20), (expired, 200)]:
        with pytest.raises(VpError) as exc:
            mapper.submit("session-1", request, presentation, now=now)
        codes.append(exc.value.code)
        messages.append(str(exc.value))
    assert codes == [INVALID_PRESENTATION, INVALID_PRESENTATION]
    assert len(set(messages)) == 1


def test_concurrent_submit_only_one_use():
    mapper = _mapper()
    request = _request(mapper)
    presentation = _presentation(request)
    barrier = threading.Barrier(2)
    successes = []
    errors = []
    lock = threading.Lock()

    def worker():
        barrier.wait()
        try:
            result = mapper.submit("session-1", request, presentation, now=20)
            with lock:
                successes.append(result["subject"])
        except VpError as exc:
            with lock:
                errors.append(exc.code)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert successes == ["cred-1"]
    assert errors == [INVALID_PRESENTATION]
