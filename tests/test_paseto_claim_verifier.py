import base64
import hashlib
import hmac
import json
import threading

import pytest

import paseto_claim_verifier
from paseto_claim_verifier import PasetoClaimVerifier, PasetoError

KEY = b"trusted-key-1"
OLD_KEY = b"trusted-key-0"


def _b64(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _token(payload, kid="kid-1", key=KEY, footer=None, implicit="",
           version="v4", purpose="public"):
    payload_b64 = _b64(json.dumps(payload, separators=(",", ":")).encode())
    footer_value = {"kid": kid}
    if footer:
        footer_value.update(footer)
    footer_b64 = _b64(json.dumps(footer_value, separators=(",", ":")).encode())
    signing = "%s.%s.%s.%s.%s" % (version, purpose, payload_b64, footer_b64, implicit)
    signature = hmac.new(key, signing.encode(), hashlib.sha256).digest()
    return "%s.%s.%s.%s.%s" % (version, purpose, payload_b64, footer_b64, _b64(signature))


def _verifier(**kwargs):
    options = dict(
        trusted_keys={
            "kid-1": {"key": KEY, "version": "v4", "purpose": "public",
                      "active": True, "grace": False},
        },
        tenant_skew={"tenant-60": 60, "tenant-0": 0},
    )
    options.update(kwargs)
    return PasetoClaimVerifier(**options)


def _payload(**overrides):
    payload = {
        "sub": "alice",
        "aud": "api.example",
        "exp": 1000000,
        "nbf": 0,
        "nonce": "nonce-1",
    }
    payload.update(overrides)
    return payload


def test_version_purpose_mismatch_rejected():
    verifier = _verifier()
    token = _token(_payload(), version="v2", purpose="local")
    with pytest.raises(PasetoError):
        verifier.verify(token, now=10)


def test_footer_tamper_breaks_signature():
    verifier = _verifier()
    token = _token(_payload(), footer={"extra": "a"})
    parts = token.split(".")
    footer = json.loads(base64.urlsafe_b64decode(parts[3] + "=="))
    footer["extra"] = "b"
    parts[3] = _b64(json.dumps(footer, separators=(",", ":")).encode())
    with pytest.raises(PasetoError):
        verifier.verify(".".join(parts), now=10)


def test_implicit_assertion_authenticated():
    verifier = _verifier()
    token = _token(_payload(), implicit="channel-binding")
    with pytest.raises(PasetoError):
        verifier.verify(token, now=10)
    assert verifier.verify(token, implicit="channel-binding", now=10)["sub"] == "alice"


def test_untrusted_kid_rejected():
    verifier = _verifier()
    token = _token(_payload(), kid="kid-x")
    with pytest.raises(PasetoError):
        verifier.verify(token, now=10)


def test_attacker_key_with_known_kid_rejected():
    verifier = _verifier()
    token = _token(_payload(), kid="kid-1", key=b"attacker-key")
    with pytest.raises(PasetoError):
        verifier.verify(token, now=10)


def test_numeric_time_comparison():
    verifier = _verifier()
    future = _token(_payload(exp=10_000_000_000, nonce="n-future"))
    assert verifier.verify(future, now=9_999_999_999)["sub"] == "alice"
    expired = _token(_payload(exp=9_999_999_999, nonce="n-past"))
    with pytest.raises(PasetoError):
        verifier.verify(expired, now=10_000_000_000)


def test_not_before_in_future_rejected():
    verifier = _verifier()
    token = _token(_payload(nbf=1000))
    with pytest.raises(PasetoError):
        verifier.verify(token, now=999)


def test_nonce_single_use():
    verifier = _verifier()
    token = _token(_payload())
    assert verifier.verify(token, now=10)["sub"] == "alice"
    with pytest.raises(PasetoError):
        verifier.verify(token, now=10)


def test_uniform_error_code_and_message():
    verifier = _verifier()
    bad_version = _token(_payload(nonce="n1"), version="v2", purpose="local")
    bad_kid = _token(_payload(nonce="n2"), kid="kid-x")
    messages = []
    for token in (bad_version, bad_kid):
        with pytest.raises(PasetoError) as exc:
            verifier.verify(token, now=10)
        assert exc.value.code == "invalid-token"
        messages.append(str(exc.value))
    assert len(set(messages)) == 1


def test_key_rotation_grace_accepts_old_key():
    verifier = _verifier(trusted_keys={
        "kid-1": {"key": KEY, "version": "v4", "purpose": "public",
                  "active": True, "grace": False},
        "kid-0": {"key": OLD_KEY, "version": "v4", "purpose": "public",
                  "active": False, "grace": True},
    })
    token = _token(_payload(nonce="old-nonce"), kid="kid-0", key=OLD_KEY)
    assert verifier.verify(token, now=10)["sub"] == "alice"


def test_retired_key_without_grace_rejected():
    verifier = _verifier(trusted_keys={
        "kid-0": {"key": OLD_KEY, "version": "v4", "purpose": "public",
                  "active": False, "grace": False},
    })
    token = _token(_payload(), kid="kid-0", key=OLD_KEY)
    with pytest.raises(PasetoError):
        verifier.verify(token, now=10)


def test_audience_array_any_match():
    verifier = _verifier()
    token = _token(_payload(aud=["other-app", "api.example"]))
    assert verifier.verify(token, audience="api.example", now=10)["sub"] == "alice"
    other = _token(_payload(aud=["other-app"], nonce="n-2"))
    with pytest.raises(PasetoError):
        verifier.verify(other, audience="api.example", now=10)


def test_oversized_token_rejected_before_parse():
    verifier = _verifier(max_token_bytes=128)
    with pytest.raises(PasetoError):
        verifier.verify("v4.public." + "a" * 256, now=10)


def test_per_tenant_clock_skew():
    verifier = _verifier()
    token = _token(_payload(exp=1000, nonce="n-skew"))
    assert verifier.verify(token, now=1030, tenant="tenant-60")["sub"] == "alice"
    with pytest.raises(PasetoError):
        verifier.verify(token, now=1030, tenant="tenant-0")


def test_concurrent_nonce_consumed_once():
    verifier = _verifier()
    token = _token(_payload(nonce="n-concurrent"))
    barrier = threading.Barrier(2)
    successes = []
    errors = []
    lock = threading.Lock()

    def worker():
        barrier.wait()
        try:
            result = verifier.verify(token, now=10)
            with lock:
                successes.append(result["sub"])
        except Exception as exc:
            with lock:
                errors.append(type(exc).__name__)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert successes == ["alice"]
    assert len(errors) == 1
