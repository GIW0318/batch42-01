import json
import threading

import pytest

import magic_link_redeemer
from magic_link_redeemer import MagicLinkError, MagicLinkRedeemer

ALLOWED = "https://app.example/callback"


def _redeemer(**kwargs):
    options = dict(secret=b"magic-secret", ttl=900,
                   redirect_allowlist=(ALLOWED,), max_requests_per_hour=5)
    options.update(kwargs)
    return MagicLinkRedeemer(**options)


def test_normalize_email_alias():
    redeemer = _redeemer()
    assert redeemer.normalize_email("Alice+promo@Example.com") == "alice@example.com"
    assert redeemer.normalize_email(" ALICE@example.com ") == "alice@example.com"


def test_preview_does_not_consume():
    redeemer = _redeemer()
    token = redeemer.issue("alice@example.com", redirect_url=ALLOWED)
    first = redeemer.preview(token, now=0)
    second = redeemer.preview(token, now=0)
    assert first["email"] == "alice@example.com"
    assert second["redirect"] == ALLOWED
    assert redeemer.redeem(token, "alice@example.com", request_id="r1")["session"]


def test_token_bound_to_issuing_email():
    redeemer = _redeemer()
    token = redeemer.issue("Alice@Example.com", redirect_url=ALLOWED)
    with pytest.raises(MagicLinkError) as exc:
        redeemer.redeem(token, "bob@example.com", request_id="r1")
    assert exc.value.code == "invalid-token"
    assert redeemer.redeem(token, "alice@example.com", request_id="r2")["session"]


def test_expiry_boundary_uses_server_clock():
    redeemer = _redeemer(ttl=900)
    token = redeemer.issue("alice@example.com", now=100)
    assert redeemer.preview(token, now=999)["email"] == "alice@example.com"
    with pytest.raises(MagicLinkError) as exc:
        redeemer.preview(token, now=1000)
    assert exc.value.code == "invalid-token"


def test_concurrent_redeem_only_one_session():
    redeemer = _redeemer()
    token = redeemer.issue("alice@example.com", redirect_url=ALLOWED)
    barrier = threading.Barrier(2)
    sessions = []
    errors = []
    lock = threading.Lock()

    def worker(request_id):
        barrier.wait()
        try:
            result = redeemer.redeem(token, "alice@example.com",
                                     request_id=request_id)
            with lock:
                sessions.append(result["session"])
        except MagicLinkError as exc:
            with lock:
                errors.append(exc.code)

    threads = [threading.Thread(target=worker, args=("r%d" % n,)) for n in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(sessions) == 1
    assert errors == ["invalid-token"]


def test_snapshot_contains_no_raw_token():
    redeemer = _redeemer()
    token = redeemer.issue("alice@example.com", redirect_url=ALLOWED)
    snapshot = redeemer.snapshot()
    assert token not in json.dumps(snapshot)


def test_resend_invalidates_old_link():
    redeemer = _redeemer()
    first = redeemer.issue("alice@example.com", redirect_url=ALLOWED)
    second = redeemer.resend("alice@example.com", redirect_url=ALLOWED)
    with pytest.raises(MagicLinkError):
        redeemer.preview(first)
    assert redeemer.preview(second)["email"] == "alice@example.com"


def test_replay_after_success_rejected():
    redeemer = _redeemer()
    token = redeemer.issue("alice@example.com", redirect_url=ALLOWED)
    redeemer.redeem(token, "alice@example.com", request_id="r1")
    with pytest.raises(MagicLinkError) as exc:
        redeemer.redeem(token, "alice@example.com", request_id="r2")
    assert exc.value.code == "invalid-token"


def test_redirect_allowlist_enforced():
    redeemer = _redeemer()
    token = redeemer.issue("alice@example.com", redirect_url=ALLOWED)
    with pytest.raises(MagicLinkError) as exc:
        redeemer.redeem(token, "alice@example.com",
                        redirect_url="https://evil.example/", request_id="r1")
    assert exc.value.code == "invalid-redirect"
    result = redeemer.redeem(token, "alice@example.com",
                             redirect_url=ALLOWED, request_id="r2")
    assert result["redirect"] == ALLOWED


def test_uniform_error_code_for_all_failures():
    redeemer = _redeemer(ttl=10)
    expired = redeemer.issue("alice@example.com", now=0)
    used = redeemer.issue("bob@example.com", now=0)
    redeemer.redeem(used, "bob@example.com", request_id="r1")
    messages = []
    for token, email, now in [
        (expired, "alice@example.com", 100),
        (used, "bob@example.com", 0),
        ("not-a-token", "carol@example.com", 0),
    ]:
        with pytest.raises(MagicLinkError) as exc:
            redeemer.redeem(token, email, now=now, request_id="rx")
        assert exc.value.code == "invalid-token"
        messages.append(str(exc.value))
    assert len(set(messages)) == 1


def test_same_request_id_idempotent():
    redeemer = _redeemer()
    token = redeemer.issue("alice@example.com", redirect_url=ALLOWED)
    first = redeemer.redeem(token, "alice@example.com", request_id="same")
    second = redeemer.redeem(token, "alice@example.com", request_id="same")
    assert first["session"] == second["session"]
    assert second["duplicate"] is True


def test_used_state_survives_restart():
    redeemer = _redeemer()
    token = redeemer.issue("alice@example.com", redirect_url=ALLOWED)
    redeemer.redeem(token, "alice@example.com", request_id="r1")
    snapshot = redeemer.snapshot()
    restored = _redeemer()
    restored.restore(json.loads(json.dumps(snapshot)))
    with pytest.raises(MagicLinkError) as exc:
        restored.redeem(token, "alice@example.com", request_id="r2")
    assert exc.value.code == "invalid-token"


def test_rate_limit_per_email_across_sources():
    redeemer = _redeemer(max_requests_per_hour=5)
    for number in range(5):
        redeemer.issue("alice@example.com", now=0,
                       source_ip="198.51.100.%d" % number)
    with pytest.raises(MagicLinkError) as exc:
        redeemer.issue("alice@example.com", now=0, source_ip="203.0.113.9")
    assert exc.value.code == "rate-limited"
    assert redeemer.request_count("Alice@Example.com") == 5


def test_rate_limit_window_resets():
    redeemer = _redeemer(max_requests_per_hour=1)
    redeemer.issue("alice@example.com", now=0)
    with pytest.raises(MagicLinkError):
        redeemer.issue("alice@example.com", now=10)
    assert redeemer.issue("alice@example.com", now=3600)


def test_allowed_redirect_returned_after_redeem():
    redeemer = _redeemer()
    token = redeemer.issue("alice@example.com", redirect_url=ALLOWED)
    result = redeemer.redeem(token, "alice@example.com", request_id="r1")
    assert result["redirect"] == ALLOWED
    assert result["email"] == "alice@example.com"
