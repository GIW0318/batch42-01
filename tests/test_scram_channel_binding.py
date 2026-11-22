import base64
import hashlib
import hmac
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from scram_channel_binding import ScramChannelBinding, ScramError
import scram_channel_binding


def credentials(password=b"pencil", salt=b"saltsalt", iterations=4096):
    salted = hashlib.pbkdf2_hmac("sha256", password, salt, iterations)
    client_key = hmac.new(salted, b"Client Key", hashlib.sha256).digest()
    return {
        "salt": salt,
        "iterations": iterations,
        "client_key": client_key,
        "stored_key": hashlib.sha256(client_key).digest(),
        "server_key": hmac.new(salted, b"Server Key", hashlib.sha256).digest(),
    }


def register(guard, username, creds):
    guard.register_user(
        username,
        salt=creds["salt"],
        stored_key=creds["stored_key"],
        server_key=creds["server_key"],
        iterations=creds["iterations"],
    )


def start_session(guard, username, creds, binding=b"tls-binding-value",
                  client_nonce="clientnonce12345"):
    client_first = "n=%s,r=%s" % (username, client_nonce)
    started = guard.start(client_first, tls_binding=binding, client_ip="192.0.2.10")
    nonce = started["nonce"]
    client_final_without_proof = "c=%s,r=%s" % (
        base64.b64encode(binding).decode(),
        nonce,
    )
    auth_message = client_first + "," + started["server_first"] + "," + client_final_without_proof
    client_signature = hmac.new(
        creds["stored_key"], auth_message.encode(), hashlib.sha256
    ).digest()
    proof = bytes(a ^ b for a, b in zip(creds["client_key"], client_signature))
    client_final = client_final_without_proof + ",p=" + base64.b64encode(proof).decode()
    return started, client_first, client_final, auth_message


def test_client_nonce_length_and_charset_are_validated():
    guard = ScramChannelBinding()
    register(guard, "alice", credentials())
    with pytest.raises(ScramError) as exc:
        guard.start("n=alice,r=short", tls_binding=b"binding", client_ip="1.1.1.1")
    assert exc.value.code == "BAD_NONCE"
    with pytest.raises(ScramError) as exc:
        guard.start("n=alice,r=bad!nonce12345", tls_binding=b"binding", client_ip="1.1.1.1")
    assert exc.value.code == "BAD_NONCE"


def test_server_nonce_is_unique_under_concurrency():
    guard = ScramChannelBinding()
    register(guard, "alice", credentials())
    barrier = threading.Barrier(16)

    def worker(index):
        barrier.wait()
        try:
            return guard.start(
                "n=alice,r=clientnonce%05d" % index,
                tls_binding=b"binding",
                client_ip="192.0.2.10",
            )["nonce"]
        except Exception:
            return ""

    with ThreadPoolExecutor(max_workers=16) as pool:
        nonces = list(pool.map(worker, range(64)))
    assert len(set(nonces)) == 64


def test_channel_binding_mismatch_fails_authentication():
    guard = ScramChannelBinding()
    register(guard, "alice", credentials())
    started = guard.start(
        "n=alice,r=clientnonce12345",
        tls_binding=b"server-export-value",
        client_ip="192.0.2.10",
    )
    client_final = "c=%s,r=%s,p=%s" % (
        base64.b64encode(b"other-binding-value").decode(),
        started["nonce"],
        base64.b64encode(b"proof").decode(),
    )
    with pytest.raises(ScramError) as exc:
        guard.finish(client_final, client_ip="192.0.2.10")
    assert exc.value.code == "CHANNEL_BINDING_MISMATCH"


def test_iterations_below_minimum_are_rejected():
    guard = ScramChannelBinding(min_iterations=4096)
    register(guard, "alice", credentials())
    with pytest.raises(ScramError) as exc:
        guard.start(
            "n=alice,r=clientnonce12345,i=1",
            tls_binding=b"binding",
            client_ip="192.0.2.10",
        )
    assert exc.value.code == "ITERATIONS_TOO_LOW"


def test_empty_salt_is_rejected_at_registration():
    guard = ScramChannelBinding()
    with pytest.raises(ScramError) as exc:
        guard.register_user(
            "alice", salt=b"", stored_key=b"stored", server_key=b"server"
        )
    assert exc.value.code == "EMPTY_SALT"


def test_auth_message_uses_rfc_field_order():
    guard = ScramChannelBinding()
    message = guard.auth_message(
        "n=user,r=clientnonce",
        "r=clientnonceservernonce,s=c2FsdA==,i=4096",
        "c=biws,r=clientnonceservernonce",
    )
    assert message == (
        "n=user,r=clientnonce,"
        "r=clientnonceservernonce,s=c2FsdA==,i=4096,"
        "c=biws,r=clientnonceservernonce"
    )


def test_wrong_proof_returns_authentication_failure():
    guard = ScramChannelBinding()
    creds = credentials()
    register(guard, "alice", creds)
    started = guard.start(
        "n=alice,r=clientnonce12345",
        tls_binding=b"binding",
        client_ip="192.0.2.10",
    )
    client_final = "c=%s,r=%s,p=%s" % (
        base64.b64encode(b"binding").decode(),
        started["nonce"],
        base64.b64encode(b"wrong-proof").decode(),
    )
    with pytest.raises(ScramError) as exc:
        guard.finish(client_final, client_ip="192.0.2.10")
    assert exc.value.code == "AUTH_FAILED"


def test_unknown_user_and_wrong_password_share_the_same_failure():
    guard = ScramChannelBinding()
    register(guard, "alice", credentials())
    try:
        guard.start(
            "n=nobody,r=clientnonce12345",
            tls_binding=b"binding",
            client_ip="192.0.2.10",
        )
    except ScramError as exc:
        unknown = (exc.code, str(exc))
    started = guard.start(
        "n=alice,r=clientnonce12345",
        tls_binding=b"binding",
        client_ip="192.0.2.10",
    )
    bad_final = "c=%s,r=%s,p=%s" % (
        base64.b64encode(b"binding").decode(),
        started["nonce"],
        base64.b64encode(b"bad").decode(),
    )
    try:
        guard.finish(bad_final, client_ip="192.0.2.10")
    except ScramError as exc:
        wrong = (exc.code, str(exc))
    assert unknown == wrong == ("AUTH_FAILED", "AUTH_FAILED")


def test_client_final_cannot_be_replayed():
    guard = ScramChannelBinding()
    creds = credentials()
    register(guard, "alice", creds)
    started, _, client_final, _ = start_session(guard, "alice", creds)
    result = guard.finish(client_final, client_ip="192.0.2.10")
    assert result["authenticated"] is True
    with pytest.raises(ScramError) as exc:
        guard.finish(client_final, client_ip="192.0.2.10")
    assert exc.value.code == "REPLAY"


def test_successful_authentication_returns_server_signature():
    guard = ScramChannelBinding()
    creds = credentials()
    register(guard, "alice", creds)
    started, _, client_final, auth_message = start_session(guard, "alice", creds)
    result = guard.finish(client_final, client_ip="192.0.2.10")
    expected = base64.b64encode(
        hmac.new(creds["server_key"], auth_message.encode(), hashlib.sha256).digest()
    ).decode()
    assert result["authenticated"] is True
    assert result["user"] == "alice"
    assert result["server_signature"] == expected


def test_failed_attempts_are_rate_limited():
    guard = ScramChannelBinding(max_failures=3)
    creds = credentials()
    register(guard, "alice", creds)
    started = guard.start(
        "n=alice,r=clientnonce12345",
        tls_binding=b"binding",
        client_ip="192.0.2.10",
    )
    bad_final = "c=%s,r=%s,p=%s" % (
        base64.b64encode(b"binding").decode(),
        started["nonce"],
        base64.b64encode(b"bad").decode(),
    )
    for _ in range(3):
        with pytest.raises(ScramError) as exc:
            guard.finish(bad_final, client_ip="192.0.2.10")
        assert exc.value.code == "AUTH_FAILED"
    with pytest.raises(ScramError) as exc:
        guard.finish(bad_final, client_ip="192.0.2.10")
    assert exc.value.code == "ERATELIMIT"


def test_password_change_invalidates_cached_verifier():
    guard = ScramChannelBinding()
    old = credentials(password=b"old-password")
    new = credentials(password=b"new-password")
    register(guard, "alice", old)
    started, _, client_final, _ = start_session(guard, "alice", old)
    guard.change_password(
        "alice",
        salt=new["salt"],
        stored_key=new["stored_key"],
        server_key=new["server_key"],
        iterations=new["iterations"],
    )
    with pytest.raises(ScramError) as exc:
        guard.finish(client_final, client_ip="192.0.2.10")
    assert exc.value.code == "AUTH_FAILED"


def test_malformed_client_final_returns_bad_request():
    guard = ScramChannelBinding()
    register(guard, "alice", credentials())
    with pytest.raises(ScramError) as exc:
        guard.finish("not-a-scram-message", client_ip="192.0.2.10")
    assert exc.value.code == "BAD_REQUEST"


def test_audit_records_nonce_iterations_and_binding_result():
    guard = ScramChannelBinding()
    creds = credentials()
    register(guard, "alice", creds)
    started, _, client_final, _ = start_session(guard, "alice", creds)
    guard.finish(client_final, client_ip="192.0.2.10")
    events = guard.audit_events()
    assert events
    event = events[-1]
    assert event["nonce"] == started["nonce"]
    assert event["iterations"] == 4096
    assert event["binding"] == "ok"
    text = str(events)
    assert base64.b64encode(b"pencil").decode() not in text
    assert client_final.split(",p=")[1] not in text


def test_channel_binding_must_match_every_byte():
    guard = ScramChannelBinding()
    creds = credentials()
    register(guard, "alice", creds)
    started = guard.start(
        "n=alice,r=clientnonce12345",
        tls_binding=b"export-12345678",
        client_ip="192.0.2.10",
    )
    client_final = "c=%s,r=%s,p=%s" % (
        base64.b64encode(b"export-12345679").decode(),
        started["nonce"],
        base64.b64encode(b"proof").decode(),
    )
    with pytest.raises(ScramError) as exc:
        guard.finish(client_final, client_ip="192.0.2.10")
    assert exc.value.code == "CHANNEL_BINDING_MISMATCH"
