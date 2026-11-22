import pytest

import ntlm_relay_fence
from ntlm_relay_fence import NtlmError, NtlmRelayFence

SPN = "HTTP/web.corp.example"
CBT = "tls-server-end-point:abc"


def _fence(**kwargs):
    return NtlmRelayFence(allowed_domains=("CORP", "PARTNER"), **kwargs)


def _begin(fence, conn="c1", spn=SPN, cbt=CBT):
    return fence.begin(conn, "10.0.0.5", spn, cbt)


def _message(challenge, **overrides):
    message = {
        "user": "alice",
        "domain": "CORP",
        "ntlm_version": 2,
        "mic_valid": True,
        "cbt": CBT,
        "spn": SPN,
        "challenge": challenge,
        "response": "response-bytes",
    }
    message.update(overrides)
    return message


def test_mic_missing_rejected():
    fence = _fence()
    challenge = _begin(fence)["challenge"]
    with pytest.raises(NtlmError) as exc:
        fence.authenticate("c1", _message(challenge, mic_valid=False))
    assert exc.value.code == "mic-required"


def test_cbt_mismatch_rejected():
    fence = _fence()
    challenge = _begin(fence)["challenge"]
    with pytest.raises(NtlmError) as exc:
        fence.authenticate("c1", _message(challenge, cbt="tls-server-end-point:other"))
    assert exc.value.code == "cbt-mismatch"


def test_spn_mismatch_rejected():
    fence = _fence()
    challenge = _begin(fence)["challenge"]
    with pytest.raises(NtlmError) as exc:
        fence.authenticate("c1", _message(challenge, spn="HTTP/other.example"))
    assert exc.value.code == "spn-mismatch"


def test_ntlmv1_rejected():
    fence = _fence()
    challenge = _begin(fence)["challenge"]
    with pytest.raises(NtlmError) as exc:
        fence.authenticate("c1", _message(challenge, ntlm_version=1))
    assert exc.value.code == "ntlmv1-rejected"


def test_challenge_reuse_across_connection_rejected():
    fence = _fence()
    first = _begin(fence, conn="c1")["challenge"]
    second = _begin(fence, conn="c2")["challenge"]
    assert first != second
    with pytest.raises(NtlmError) as exc:
        fence.authenticate("c2", _message(first))
    assert exc.value.code == "challenge-reuse"


def test_duplicate_authentication_idempotent():
    fence = _fence()
    challenge = _begin(fence)["challenge"]
    first = fence.authenticate("c1", _message(challenge))
    second = fence.authenticate("c1", _message(challenge))
    assert first["authenticated"] is True
    assert second["duplicate"] is True
    assert second["user"] == first["user"]


def test_second_negotiation_resets_state():
    fence = _fence()
    old_challenge = _begin(fence, conn="c1")["challenge"]
    new_challenge = fence.begin("c1", "10.0.0.5", SPN, CBT)["challenge"]
    assert new_challenge != old_challenge
    with pytest.raises(NtlmError) as exc:
        fence.authenticate("c1", _message(old_challenge))
    assert exc.value.code == "challenge-reuse"
    assert fence.authenticate("c1", _message(new_challenge))["authenticated"] is True


def test_anonymous_rejected():
    fence = _fence()
    challenge = _begin(fence)["challenge"]
    with pytest.raises(NtlmError) as exc:
        fence.authenticate("c1", _message(challenge, user="anonymous"))
    assert exc.value.code == "anonymous-rejected"


def test_guest_rejected():
    fence = _fence()
    challenge = _begin(fence)["challenge"]
    with pytest.raises(NtlmError) as exc:
        fence.authenticate("c1", _message(challenge, user="guest"))
    assert exc.value.code == "guest-rejected"


def test_failed_auth_closes_connection():
    fence = _fence()
    challenge = _begin(fence)["challenge"]
    with pytest.raises(NtlmError):
        fence.authenticate("c1", _message(challenge, mic_valid=False))
    assert fence.connection_state("c1") == "closed"
    with pytest.raises(NtlmError) as exc:
        fence.authenticate("c1", _message(challenge))
    assert exc.value.code == "connection-closed"


def test_cross_domain_referral_rejected():
    fence = _fence()
    challenge = _begin(fence)["challenge"]
    with pytest.raises(NtlmError) as exc:
        fence.authenticate("c1", _message(challenge, domain="EVIL"))
    assert exc.value.code == "cross-domain-referral"


def test_audit_records_source_and_target_spn():
    fence = _fence()
    challenge = _begin(fence)["challenge"]
    fence.authenticate("c1", _message(challenge))
    entry = fence.audit_log()[-1]
    assert entry["source_ip"] == "10.0.0.5"
    assert entry["target_spn"] == SPN
    assert entry["decision"] == "authenticated"


def test_successful_authentication_returns_user():
    fence = _fence()
    challenge = _begin(fence)["challenge"]
    result = fence.authenticate("c1", _message(challenge))
    assert result["authenticated"] is True
    assert result["user"] == "CORP\\alice"
    assert fence.connection_state("c1") == "authenticated"


def test_allowed_partner_domain_accepted():
    fence = _fence()
    challenge = _begin(fence)["challenge"]
    result = fence.authenticate("c1", _message(challenge, domain="PARTNER"))
    assert result["user"] == "PARTNER\\alice"


def test_unknown_connection_rejected():
    fence = _fence()
    with pytest.raises(NtlmError) as exc:
        fence.authenticate("missing", _message("challenge"))
    assert exc.value.code == "unknown-connection"
