import threading
from concurrent.futures import ThreadPoolExecutor

from dns_cookie_verifier import DnsCookieVerifier
import dns_cookie_verifier


def test_udp_query_without_cookie_gets_badcookie_and_new_cookie():
    verifier = DnsCookieVerifier()
    result = verifier.verify(None, None, "192.0.2.10", udp=True, now=0.0)
    assert result["status"] == "badcookie"
    assert result["cookie"]
    assert result["reason"] == "NO_COOKIE"


def test_client_cookie_without_server_cookie_is_dropped():
    verifier = DnsCookieVerifier()
    result = verifier.verify(b"12345678", None, "192.0.2.10", udp=True, now=0.0)
    assert result["status"] == "drop"
    assert result["cookie"] is None


def test_server_cookie_mismatch_is_silently_dropped():
    verifier = DnsCookieVerifier()
    client_cookie = b"12345678"
    result = verifier.verify(client_cookie, b"Z" * 16, "192.0.2.10", now=0.0)
    assert result["status"] == "drop"
    assert result["cookie"] is None
    assert result["reason"] == "BAD_SERVER_COOKIE"


def test_valid_cookie_is_accepted():
    verifier = DnsCookieVerifier()
    client_cookie = b"abcdefgh"
    server_cookie = verifier.make_server_cookie("192.0.2.10", client_cookie, now=0.0)
    result = verifier.verify(client_cookie, server_cookie, "192.0.2.10", now=1.0)
    assert result["status"] == "ok"
    assert result["key_version"] == 1


def test_rotated_key_keeps_old_cookie_valid_during_overlap():
    verifier = DnsCookieVerifier(cookie_ttl=10000, rotation_grace=300)
    client_cookie = b"rotate01"
    old_cookie = verifier.make_server_cookie("192.0.2.10", client_cookie, now=0.0)
    verifier.rotate_secret(b"new-secret-0123456789abcdef", now=10.0)
    assert verifier.verify(client_cookie, old_cookie, "192.0.2.10", now=100.0)["status"] == "ok"
    assert verifier.verify(client_cookie, old_cookie, "192.0.2.10", now=400.0)["status"] == "badcookie"


def test_restart_restores_keys_from_snapshot():
    verifier = DnsCookieVerifier()
    client_cookie = b"restart1"
    cookie = verifier.make_server_cookie("192.0.2.10", client_cookie, now=0.0)
    snapshot = verifier.snapshot()
    restored = DnsCookieVerifier()
    restored.restore(snapshot)
    result = restored.verify(client_cookie, cookie, "192.0.2.10", now=1.0)
    assert result["status"] == "ok"


def test_expired_cookie_is_rejected():
    verifier = DnsCookieVerifier(cookie_ttl=60)
    client_cookie = b"expire01"
    cookie = verifier.make_server_cookie("192.0.2.10", client_cookie, now=0.0)
    assert verifier.verify(client_cookie, cookie, "192.0.2.10", now=30.0)["status"] == "ok"
    expired = verifier.verify(client_cookie, cookie, "192.0.2.10", now=61.0)
    assert expired["status"] == "badcookie"
    assert expired["reason"] == "EXPIRED"


def test_invalid_cookie_length_returns_formerr_instead_of_crashing():
    verifier = DnsCookieVerifier()
    short = verifier.verify(b"short", b"x" * 16, "192.0.2.10", now=0.0)
    oversized = verifier.verify(b"a" * 40, b"x" * 16, "192.0.2.10", now=0.0)
    assert short["status"] == "formerr"
    assert oversized["status"] == "formerr"


def test_nonces_are_unique_under_concurrency():
    verifier = DnsCookieVerifier()
    barrier = threading.Barrier(16)

    def worker(_):
        barrier.wait()
        try:
            return verifier.nonce("192.0.2.10")
        except Exception:
            return b""

    with ThreadPoolExecutor(max_workers=16) as pool:
        nonces = list(pool.map(worker, range(128)))
    assert len(set(nonces)) == 128


def test_nat_clients_with_same_ip_have_independent_cookies():
    verifier = DnsCookieVerifier()
    first_client = b"client01"
    second_client = b"client02"
    first_server = verifier.make_server_cookie("198.51.100.7", first_client, now=0.0)
    second_server = verifier.make_server_cookie("198.51.100.7", second_client, now=0.0)
    assert verifier.verify(first_client, first_server, "198.51.100.7", now=1.0)["status"] == "ok"
    assert verifier.verify(second_client, second_server, "198.51.100.7", now=1.0)["status"] == "ok"


def test_ixfr_query_uses_same_cookie_policy():
    verifier = DnsCookieVerifier()
    result = verifier.verify(None, None, "192.0.2.10", udp=True, qtype="IXFR", now=0.0)
    assert result["status"] == "badcookie"
    assert result["cookie"]


def test_new_cookie_reports_rotated_key_version():
    verifier = DnsCookieVerifier()
    verifier.rotate_secret(b"version2-secret-0123456789", now=0.0)
    client_cookie = b"version2"
    cookie = verifier.make_server_cookie("192.0.2.10", client_cookie, now=10.0)
    result = verifier.verify(client_cookie, cookie, "192.0.2.10", now=11.0)
    assert result["status"] == "ok"
    assert result["key_version"] == 2


def test_audit_log_does_not_contain_full_cookie():
    verifier = DnsCookieVerifier()
    client_cookie = b"secretck"
    cookie = verifier.make_server_cookie("192.0.2.10", client_cookie, now=0.0)
    verifier.verify(client_cookie, cookie, "192.0.2.10", now=1.0)
    text = str(verifier.audit_events())
    assert cookie.hex() not in text
    assert client_cookie.hex() not in text


def test_badcookie_response_echoes_client_cookie():
    verifier = DnsCookieVerifier(cookie_ttl=10)
    client_cookie = b"echo1234"
    cookie = verifier.make_server_cookie("192.0.2.10", client_cookie, now=0.0)
    result = verifier.verify(client_cookie, cookie, "192.0.2.10", now=11.0)
    assert result["status"] == "badcookie"
    assert result["cookie"][: len(client_cookie)] == client_cookie


def test_cookie_is_bound_to_client_address():
    verifier = DnsCookieVerifier()
    client_cookie = b"bind0001"
    cookie = verifier.make_server_cookie("192.0.2.10", client_cookie, now=0.0)
    result = verifier.verify(client_cookie, cookie, "198.51.100.20", now=1.0)
    assert result["status"] == "drop"
    assert result["reason"] == "CLIENT_MISMATCH"
