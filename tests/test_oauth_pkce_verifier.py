import base64
import hashlib
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from oauth_pkce_verifier import GrantExchangeGate, OAuthError
import oauth_pkce_verifier

REDIRECT = "https://app.example/cb"


def b64url(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def pkce_pair(verifier=None):
    verifier = verifier or "A" * 43
    challenge = b64url(hashlib.sha256(verifier.encode()).digest())
    return verifier, challenge


def make_gate():
    gate = GrantExchangeGate()
    gate.register_client("app", redirect_uris=[REDIRECT])
    return gate


def issue(gate, scope="read", now=0.0):
    verifier, challenge = pkce_pair()
    grant = gate.authorize(
        "app", REDIRECT, scope, code_challenge=challenge, method="S256", now=now
    )
    return verifier, grant["code"]


def test_public_client_without_challenge_is_rejected():
    gate = make_gate()
    with pytest.raises(OAuthError) as exc:
        gate.authorize("app", REDIRECT, "read", code_challenge=None, method="S256")
    assert exc.value.code == "pkce_required"


def test_plain_challenge_method_is_rejected():
    gate = make_gate()
    _, challenge = pkce_pair()
    with pytest.raises(OAuthError) as exc:
        gate.authorize(
            "app", REDIRECT, "read", code_challenge=challenge, method="plain"
        )
    assert exc.value.code == "invalid_request"


def test_verifier_length_and_charset_are_enforced():
    gate = make_gate()
    verifier, challenge = pkce_pair()
    first = gate.authorize(
        "app", REDIRECT, "read", code_challenge=challenge, method="S256"
    )
    with pytest.raises(OAuthError) as exc:
        gate.exchange(
            first["code"], "app", REDIRECT, code_verifier="short", now=1.0
        )
    assert exc.value.code == "bad_verifier"
    second = gate.authorize(
        "app", REDIRECT, "read", code_challenge=challenge, method="S256"
    )
    with pytest.raises(OAuthError) as exc:
        gate.exchange(
            second["code"], "app", REDIRECT, code_verifier="A" * 42 + "!", now=1.0
        )
    assert exc.value.code == "bad_verifier"


def test_concurrent_code_exchange_succeeds_only_once():
    gate = make_gate()
    verifier, code = issue(gate)
    barrier = threading.Barrier(2)

    def worker(_):
        barrier.wait()
        try:
            gate.exchange(code, "app", REDIRECT, code_verifier=verifier, now=1.0)
        except OAuthError as exc:
            return exc.code
        return "ok"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(worker, range(2)))
    assert results.count("ok") == 1
    assert results.count("code_reused") == 1


def test_bad_verifier_burns_the_authorization_code():
    gate = make_gate()
    verifier, code = issue(gate)
    with pytest.raises(OAuthError) as exc:
        gate.exchange(code, "app", REDIRECT, code_verifier="B" * 43, now=1.0)
    assert exc.value.code == "bad_verifier"
    with pytest.raises(OAuthError) as exc:
        gate.exchange(code, "app", REDIRECT, code_verifier=verifier, now=1.0)
    assert exc.value.code == "code_reused"


def test_redirect_uri_requires_exact_match():
    gate = make_gate()
    verifier, code = issue(gate)
    with pytest.raises(OAuthError) as exc:
        gate.exchange(
            code, "app", REDIRECT + "/", code_verifier=verifier, now=1.0
        )
    assert exc.value.code == "invalid_grant"


def test_code_is_bound_to_the_client():
    gate = make_gate()
    gate.register_client("other", redirect_uris=[REDIRECT])
    verifier, code = issue(gate)
    with pytest.raises(OAuthError) as exc:
        gate.exchange(code, "other", REDIRECT, code_verifier=verifier, now=1.0)
    assert exc.value.code == "invalid_grant"


def test_code_expires_after_ten_minutes():
    gate = GrantExchangeGate(code_ttl=600)
    gate.register_client("app", redirect_uris=[REDIRECT])
    verifier, code = issue(gate, now=0.0)
    with pytest.raises(OAuthError) as exc:
        gate.exchange(code, "app", REDIRECT, code_verifier=verifier, now=601.0)
    assert exc.value.code == "expired"


def test_exchange_cannot_escalate_scope():
    gate = make_gate()
    verifier, code = issue(gate, scope="read")
    tokens = gate.exchange(code, "app", REDIRECT, code_verifier=verifier, now=1.0)
    assert tokens["scope"] == "read"
    with pytest.raises(OAuthError) as exc:
        gate.refresh(tokens["refresh_token"], "app", scope="write", now=2.0)
    assert exc.value.code == "invalid_scope"


def test_refresh_rotation_revokes_the_whole_chain_on_reuse():
    gate = make_gate()
    verifier, code = issue(gate)
    tokens = gate.exchange(code, "app", REDIRECT, code_verifier=verifier, now=1.0)
    rotated = gate.refresh(tokens["refresh_token"], "app", now=2.0)
    assert rotated["refresh_token"] != tokens["refresh_token"]
    with pytest.raises(OAuthError) as exc:
        gate.refresh(tokens["refresh_token"], "app", now=3.0)
    assert exc.value.code == "token_reuse"
    with pytest.raises(OAuthError) as exc:
        gate.refresh(rotated["refresh_token"], "app", now=3.0)
    assert exc.value.code == "revoked"


def test_restart_preserves_authorization_code_state():
    gate = make_gate()
    verifier, code = issue(gate)
    snapshot = gate.snapshot()
    restored = GrantExchangeGate()
    restored.restore(snapshot)
    tokens = restored.exchange(
        code, "app", REDIRECT, code_verifier=verifier, now=1.0
    )
    assert tokens["access_token"]


def test_authorization_code_is_single_use():
    gate = make_gate()
    verifier, code = issue(gate)
    gate.exchange(code, "app", REDIRECT, code_verifier=verifier, now=1.0)
    with pytest.raises(OAuthError) as exc:
        gate.exchange(code, "app", REDIRECT, code_verifier=verifier, now=2.0)
    assert exc.value.code == "code_reused"


def test_audit_log_does_not_contain_verifier():
    gate = make_gate()
    verifier, code = issue(gate)
    gate.exchange(code, "app", REDIRECT, code_verifier=verifier, now=1.0)
    text = str(gate.audit_events())
    assert verifier not in text
    assert code not in text


def test_unregistered_redirect_uri_is_rejected():
    gate = make_gate()
    _, challenge = pkce_pair()
    with pytest.raises(OAuthError) as exc:
        gate.authorize(
            "app",
            "https://evil.example/cb",
            "read",
            code_challenge=challenge,
            method="S256",
        )
    assert exc.value.code == "invalid_redirect"


def test_s256_challenge_must_have_valid_length():
    gate = make_gate()
    with pytest.raises(OAuthError) as exc:
        gate.authorize(
            "app", REDIRECT, "read", code_challenge="x" * 42, method="S256"
        )
    assert exc.value.code == "invalid_request"
