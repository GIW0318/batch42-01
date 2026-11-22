import threading

import pytest

import dtls_handshake_guard as dhg


FT = ("10.0.0.1", 5000)


def new_guard(**kwargs):
    return dhg.DtlsHandshakeGuard("cookie-secret", **kwargs)


def hello(random_value="r1", cookie=None):
    data = {"client_random": random_value}
    if cookie is not None:
        data["cookie"] = cookie
    return data


def establish(guard, five_tuple=FT, random_value="r1"):
    first = guard.client_hello(five_tuple, hello(random_value), now=0.0)
    second = guard.client_hello(
        five_tuple, hello(random_value, first["cookie"]), now=0.1
    )
    return second["session_id"], first["cookie"]


def cert_chain(leaf_not_after=100):
    return [
        {
            "subject": "leaf",
            "issuer": "inter",
            "not_before": 0,
            "not_after": leaf_not_after,
            "ca": False,
        },
        {
            "subject": "inter",
            "issuer": "root",
            "not_before": 0,
            "not_after": 200,
            "ca": True,
        },
        {
            "subject": "root",
            "issuer": "root",
            "not_before": 0,
            "not_after": 200,
            "ca": True,
        },
    ]


def test_client_hello_without_cookie_gets_verify_request():
    guard = new_guard()
    result = guard.client_hello(FT, hello(), now=0.0)
    assert result["action"] == "hello_verify_request"
    assert isinstance(result["cookie"], str) and result["cookie"]
    assert result["session_id"] is None
    assert guard.session_count() == 0


def test_cookie_is_bound_to_transport_parameters():
    guard = new_guard()
    first = guard.client_hello(FT, hello(), now=0.0)
    result = guard.client_hello(
        ("10.0.0.1", 5001), hello(cookie=first["cookie"]), now=0.1
    )
    assert result["action"] == "reject"
    assert result["reason"] == "cookie_transport_mismatch"
    assert guard.session_count() == 0


def test_duplicate_client_hello_with_same_cookie_is_rejected():
    guard = new_guard()
    session_id, cookie = establish(guard)
    result = guard.client_hello(FT, hello(cookie=cookie), now=1.0)
    assert result["action"] == "reject"
    assert result["reason"] == "duplicate_client_hello"
    assert guard.session_count() == 1
    assert guard.session_id_for(FT) == session_id


def test_concurrent_duplicate_client_hello_creates_one_session():
    guard = new_guard()
    cookie = guard.client_hello(FT, hello(), now=0.0)["cookie"]
    barrier = threading.Barrier(2)
    results = []
    results_lock = threading.Lock()

    def call():
        barrier.wait()
        result = guard.client_hello(FT, hello(cookie=cookie), now=0.1)
        with results_lock:
            results.append(result)

    threads = [threading.Thread(target=call) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)

    assert len(results) == 2
    assert sorted(item["action"] for item in results) == ["reject", "server_hello"]
    assert guard.session_count() == 1


def test_same_nat_ip_different_ports_get_separate_sessions():
    guard = new_guard()
    first_tuple = ("10.0.0.7", 40001)
    second_tuple = ("10.0.0.7", 40002)
    first_cookie = guard.client_hello(first_tuple, hello("ra"), now=0.0)["cookie"]
    second_cookie = guard.client_hello(second_tuple, hello("rb"), now=0.0)["cookie"]
    first_session = guard.client_hello(
        first_tuple, hello("ra", first_cookie), now=0.1
    )["session_id"]
    assert guard.session_id_for(second_tuple) is None
    second_session = guard.client_hello(
        second_tuple, hello("rb", second_cookie), now=0.1
    )["session_id"]
    assert first_session != second_session
    assert guard.session_count() == 2
    assert guard.session_id_for(first_tuple) == first_session
    assert guard.session_id_for(second_tuple) == second_session


def test_retransmission_delay_uses_rtt_and_backs_off():
    guard = new_guard()
    session_id, _ = establish(guard)
    guard.note_rtt(session_id, 1.5)
    delay0 = guard.retransmission_delay(session_id, 0)
    delay1 = guard.retransmission_delay(session_id, 1)
    delay2 = guard.retransmission_delay(session_id, 2)
    assert delay0 >= 3.0
    assert delay1 > delay0
    assert delay2 > delay1


def test_application_data_before_handshake_complete_is_dropped_with_alert():
    guard = new_guard()
    session_id, _ = establish(guard)
    result = guard.application_data(session_id, 1, b"early", now=1.0)
    assert result["accepted"] is False
    assert result["alert"] == "unexpected_message"
    assert result["closed"] is False
    guard.finish_handshake(session_id)
    accepted = guard.application_data(session_id, 2, b"late", now=2.0)
    assert accepted["accepted"] is True
    assert accepted["alert"] is None


def test_early_application_data_rejections_are_bounded():
    guard = new_guard(max_early_data=2)
    session_id, _ = establish(guard)
    first = guard.application_data(session_id, 1, b"a", now=1.0)
    second = guard.application_data(session_id, 2, b"b", now=2.0)
    third = guard.application_data(session_id, 3, b"c", now=3.0)
    assert first["closed"] is False
    assert second["closed"] is False
    assert third["closed"] is True
    assert guard.early_data_rejections(session_id) == 3
    assert guard.connection_state(session_id) == "closed"


def test_out_of_order_record_cannot_regress_receive_window():
    guard = new_guard()
    session_id, _ = establish(guard)
    guard.finish_handshake(session_id)
    newer = guard.receive_record(session_id, 0, 5, b"new")
    older = guard.receive_record(session_id, 0, 3, b"old")
    replay = guard.receive_record(session_id, 0, 3, b"old")
    assert newer["accepted"] is True
    assert older["accepted"] is True
    assert older["reason"] == "out_of_order"
    assert replay["accepted"] is False
    assert replay["replay"] is True
    assert guard.receive_high_water(session_id) == 5


def test_key_update_closes_old_epoch_window():
    guard = new_guard()
    session_id, _ = establish(guard)
    guard.finish_handshake(session_id)
    assert guard.current_epoch(session_id) == 0
    assert guard.receive_record(session_id, 0, 1, b"a")["accepted"] is True
    guard.key_update(session_id)
    assert guard.current_epoch(session_id) == 1
    stale = guard.receive_record(session_id, 0, 2, b"b")
    assert stale["accepted"] is False
    assert stale["reason"] == "stale_epoch"
    assert guard.receive_record(session_id, 1, 1, b"c")["accepted"] is True


def test_protect_never_reuses_nonce_under_same_key():
    guard = new_guard()
    session_id, _ = establish(guard)
    guard.finish_handshake(session_id)
    records = [guard.protect(session_id, b"same-plaintext") for _ in range(200)]
    nonces = [item["nonce"] for item in records]
    ciphertexts = [item["ciphertext"] for item in records]
    assert len(set(nonces)) == 200
    assert len(set(ciphertexts)) == 200
    assert {item["epoch"] for item in records} == {0}


def test_restart_restores_cookie_secret_and_pending_handshake():
    guard = new_guard()
    first = guard.client_hello(FT, hello(), now=0.0)
    state = guard.export_state()
    restarted = dhg.DtlsHandshakeGuard("brand-new-secret")
    restarted.load_state(state)
    result = restarted.client_hello(FT, hello(cookie=first["cookie"]), now=1.0)
    assert result["action"] == "server_hello"
    assert result["session_id"]
    assert restarted.session_count() == 1
    restarted.finish_handshake(result["session_id"])
    assert restarted.connection_state(result["session_id"]) == "established"


def test_fragment_budget_is_hard_capped():
    guard = new_guard(max_fragment_bytes=1024)
    session_id, _ = establish(guard)
    with pytest.raises(dhg.FragmentLimitExceeded):
        guard.fragment(session_id, 1, 0, 2048, b"x" * 100)
    guard.fragment(session_id, 2, 0, 1000, b"x" * 600)
    with pytest.raises(dhg.FragmentLimitExceeded):
        guard.fragment(session_id, 2, 600, 1000, b"y" * 600)


def test_certificate_chain_requires_intermediate_and_validity_on_resume():
    guard = new_guard()
    assert guard.verify_chain(cert_chain(), now=50)["valid"] is True
    missing_intermediate = [cert_chain()[0], cert_chain()[2]]
    verdict = guard.verify_chain(missing_intermediate, now=50)
    assert verdict["valid"] is False
    assert verdict["reason"] == "incomplete_chain"
    expired = guard.verify_chain(cert_chain(leaf_not_after=20), now=50)
    assert expired["valid"] is False
    assert expired["reason"] == "certificate_expired"
    ticket = {"signature": "valid", "session_id": "s-old"}
    resumed = guard.resume_session(ticket, cert_chain(leaf_not_after=20), now=50)
    assert resumed["resumed"] is False
    assert resumed["reason"] == "certificate_expired"


def test_close_releases_socket_in_finite_steps():
    guard = new_guard()
    session_id, _ = establish(guard)
    assert guard.socket_count() == 1
    result = guard.close(session_id)
    assert result["state"] == "closed"
    assert result["socket_released"] is True
    assert guard.socket_count() == 0
    again = guard.close(session_id)
    assert again["state"] == "closed"
    assert guard.connection_state(session_id) == "closed"
