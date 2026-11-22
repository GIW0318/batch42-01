import ssh_kex_rekey_guard as skg


STRONG = "curve25519-sha256"


def new_guard(**kwargs):
    return skg.SshRekeyGuard(**kwargs)


def open_conn(guard, conn_id="c1", algorithms=None):
    algorithms = algorithms or [STRONG]
    return guard.open_connection(conn_id, algorithms, algorithms)


def rekeyed(guard, conn_id="c1", now=0.0):
    request = guard.begin_rekey(conn_id, initiator="client", now=now)
    assert request["accepted"] is True
    done = guard.complete_rekey(conn_id, request["rekey_id"])
    assert done["completed"] is True
    return done


def test_application_data_is_blocked_until_newkeys():
    guard = new_guard()
    assert open_conn(guard)["state"] == "established"
    assert guard.send_data("c1", 100)["sent"] is True
    request = guard.begin_rekey("c1", initiator="client", now=0.0)
    assert request["accepted"] is True
    blocked = guard.send_data("c1", 100)
    assert blocked["sent"] is False
    assert blocked["buffered"] is True
    assert blocked["violation"] is True
    assert guard.violations("c1") == 1
    assert guard.active_key_id("c1") == "k1"


def test_application_data_resumes_after_newkeys():
    guard = new_guard()
    open_conn(guard)
    done = rekeyed(guard)
    assert done["key_id"] == "k2"
    assert guard.active_key_id("c1") == "k2"
    resumed = guard.send_data("c1", 64)
    assert resumed["sent"] is True
    assert resumed["violation"] is False
    assert guard.violations("c1") == 0


def test_concurrent_rekey_allows_single_winner():
    guard = new_guard()
    open_conn(guard)
    first = guard.begin_rekey("c1", initiator="client", now=0.0)
    second = guard.begin_rekey("c1", initiator="server", now=0.1)
    assert first["accepted"] is True
    assert second["accepted"] is False
    assert second["reason"] == "rekey in progress"
    assert guard.pending_rekeys() == 1


def test_strongest_common_algorithm_is_selected():
    guard = new_guard()
    result = guard.open_connection(
        "c1",
        ["diffie-hellman-group14-sha256", STRONG],
        [STRONG, "diffie-hellman-group14-sha256"],
    )
    assert result["state"] == "established"
    assert result["algorithm"] == STRONG


def test_no_strong_common_algorithm_disconnects_with_exact_reason():
    guard = new_guard()
    result = guard.open_connection(
        "c1",
        ["diffie-hellman-group1-sha1"],
        ["diffie-hellman-group1-sha1"],
    )
    assert result["state"] == "disconnected"
    assert result["reason"] == "no matching key exchange method"
    assert result["algorithm"] is None
    assert guard.is_open("c1") is False


def test_channel_window_is_frozen_during_rekey_and_recomputed_after():
    guard = new_guard()
    open_conn(guard)
    guard.open_channel("c1", "ch1", window=1024)
    first = guard.consume_window("ch1", 200)
    assert first["accepted"] is True
    assert first["window"] == 824
    guard.begin_rekey("c1", initiator="client", now=0.0)
    frozen = guard.consume_window("ch1", 100)
    assert frozen["accepted"] is False
    assert frozen["reason"] == "window frozen"
    assert frozen["window"] == 824
    assert guard.channel_window("ch1") == 824
    request = guard.begin_rekey("c1", initiator="server", now=0.5)
    assert request["accepted"] is False
    pending = guard.pending_rekeys()
    assert pending == 1


def test_sequence_numbers_survive_rekey():
    guard = new_guard()
    open_conn(guard)
    guard.send_data("c1", 1)
    guard.send_data("c1", 1)
    guard.send_data("c1", 1)
    before = guard.next_sequence("c1")
    assert before == 3
    rekeyed(guard)
    assert guard.next_sequence("c1") == before
    guard.send_data("c1", 1)
    assert guard.next_sequence("c1") == before + 1


def test_weak_group1_is_never_selected_even_when_listed_first():
    guard = new_guard()
    result = guard.open_connection(
        "c1",
        ["diffie-hellman-group1-sha1", "diffie-hellman-group14-sha256"],
        ["diffie-hellman-group1-sha1", "diffie-hellman-group14-sha256"],
    )
    assert result["algorithm"] == "diffie-hellman-group14-sha256"
    assert result["algorithm"] != "diffie-hellman-group1-sha1"


def test_rekey_timeout_closes_connection_and_releases_channels():
    guard = new_guard(rekey_timeout=30.0)
    open_conn(guard)
    guard.open_channel("c1", "ch1")
    guard.begin_rekey("c1", initiator="client", now=0.0)
    result = guard.check_timeout(100.0)
    assert "c1" in result["aborted"]
    assert "c1" in result["closed"]
    assert guard.is_open("c1") is False
    assert guard.pending_rekeys() == 0
    assert guard.channel_count("c1") == 0
    assert guard.handle_count() == 0


def test_channel_close_during_rekey_releases_handle():
    guard = new_guard()
    open_conn(guard)
    guard.open_channel("c1", "ch1")
    guard.open_channel("c1", "ch2")
    assert guard.channel_count("c1") == 2
    request = guard.begin_rekey("c1", initiator="client", now=0.0)
    closed = guard.close_channel("c1", "ch1")
    assert closed["closed"] is True
    assert guard.channel_count("c1") == 1
    rekey_done = guard.complete_rekey("c1", request["rekey_id"])
    assert rekey_done["completed"] is True
    guard.close_channel("c1", "ch2")
    assert guard.channel_count("c1") == 0
    assert guard.handle_count() == 0


def test_rekey_queue_is_bounded_under_bulk_load():
    guard = new_guard(max_pending=4)
    for index in range(10):
        open_conn(guard, conn_id="c{}".format(index))
    results = [
        guard.begin_rekey("c{}".format(index), initiator="client", now=0.0)
        for index in range(10)
    ]
    accepted = [item for item in results if item["accepted"]]
    rejected = [item for item in results if not item["accepted"]]
    assert len(accepted) == 4
    assert len(rejected) == 6
    assert {item["reason"] for item in rejected} == {"rekey queue full"}
    assert guard.pending_rekeys() == 4


def test_32_bit_counter_wrap_still_triggers_rekey():
    guard = new_guard()
    open_conn(guard)
    first = guard.advance_bytes("c1", 2 ** 32 - 10)
    assert first["needs_rekey"] is False
    second = guard.advance_bytes("c1", 5)
    assert second["needs_rekey"] is True
    assert guard.counter_bytes("c1") == 2 ** 32 - 5
    assert guard.counter_bytes("c1") != 0


def test_host_key_rotation_rejects_previous_key():
    guard = new_guard()
    guard.trust_host_key("web1", "key-A")
    assert guard.check_host_key("web1", "key-A")["trusted"] is True
    guard.rotate_host_key("web1", "key-B")
    stale = guard.check_host_key("web1", "key-A")
    assert stale["trusted"] is False
    assert stale["reason"] == "host key changed"
    assert guard.check_host_key("web1", "key-B")["trusted"] is True


def test_debug_log_redacts_kex_payload_and_session_id():
    guard = new_guard()
    guard.set_debug(True)
    guard.open_connection(
        "c1",
        [STRONG],
        [STRONG],
        session_id="SESSION-SECRET-9",
    )
    guard.begin_rekey(
        "c1", initiator="client", kexinit="KEX-PAYLOAD-SECRET-RAW", now=0.0
    )
    entries = guard.debug_entries()
    assert entries
    dumped = str(entries)
    assert "SESSION-SECRET-9" not in dumped
    assert "KEX-PAYLOAD-SECRET-RAW" not in dumped


def test_byte_threshold_triggers_rekey():
    guard = new_guard()
    open_conn(guard)
    below = guard.advance_bytes("c1", 1024 * 1024 - 1)
    assert below["needs_rekey"] is False
    at_limit = guard.advance_bytes("c1", 1)
    assert at_limit["needs_rekey"] is True
    assert guard.needs_rekey("c1") is True
    assert guard.counter_bytes("c1") == 1024 * 1024
