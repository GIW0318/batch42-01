import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from srtp_key_epoch import SrtpEpochKeeper, SrtpError
import srtp_key_epoch

KEY1 = b"0123456789abcdef"
KEY2 = b"fedcba9876543210"
KEY3 = b"aaaabbbbccccdddd"


def test_new_key_coexists_with_old_until_confirmed():
    keeper = SrtpEpochKeeper()
    keeper.add_stream(1)
    keeper.install_key(1, 1, KEY1, active=True)
    old_packet = keeper.protect(1, 1, b"hello")
    keeper.install_key(1, 2, KEY2)
    keeper.activate(1, 2)
    assert keeper.active_epoch(1) == 2
    assert keeper.unprotect(1, old_packet) == b"hello"
    keeper.confirm(1, 2)
    keeper.retire(1, 1)
    with pytest.raises(SrtpError) as exc:
        keeper.unprotect(1, old_packet)
    assert exc.value.code == "EKEY"


def test_out_of_order_packets_decrypt_correctly():
    keeper = SrtpEpochKeeper()
    keeper.add_stream(1)
    keeper.install_key(1, 1, KEY1, active=True)
    later = keeper.protect(1, 5, b"five")
    earlier = keeper.protect(1, 3, b"three")
    assert keeper.unprotect(1, earlier) == b"three"
    assert keeper.unprotect(1, later) == b"five"


def test_replay_window_is_per_synchronization_source():
    keeper = SrtpEpochKeeper()
    keeper.add_stream(11)
    keeper.add_stream(22)
    keeper.install_key(11, 1, KEY1, active=True)
    keeper.install_key(22, 1, KEY2, active=True)
    first = keeper.protect(11, 1, b"first")
    second = keeper.protect(22, 1, b"second")
    assert keeper.unprotect(11, first) == b"first"
    assert keeper.unprotect(22, second) == b"second"
    assert keeper.replay_count(11) == 0
    assert keeper.replay_count(22) == 0


def test_same_ssrc_from_second_address_is_rejected():
    keeper = SrtpEpochKeeper()
    keeper.add_stream(1)
    keeper.install_key(1, 1, KEY1, active=True)
    assert keeper.bind_address(1, "192.0.2.10") is True
    with pytest.raises(SrtpError) as exc:
        keeper.bind_address(1, "198.51.100.10")
    assert exc.value.code == "EADDRINUSE"
    packet = keeper.protect(1, 1, b"media")
    with pytest.raises(SrtpError) as exc:
        keeper.unprotect(1, packet, src_addr="198.51.100.10")
    assert exc.value.code == "EADDR"


def test_packet_without_key_identifier_is_rejected():
    keeper = SrtpEpochKeeper()
    keeper.add_stream(1)
    keeper.install_key(1, 1, KEY1, active=True)
    with pytest.raises(SrtpError) as exc:
        keeper.unprotect(1, {"ssrc": 1, "seq": 1, "ciphertext": b"x"})
    assert exc.value.code == "EKEY"


def test_restart_restores_keys_and_replay_state():
    keeper = SrtpEpochKeeper()
    keeper.add_stream(1)
    keeper.install_key(1, 1, KEY1, active=True)
    packet = keeper.protect(1, 9, b"nine")
    keeper.unprotect(1, packet)
    saved = keeper.snapshot()
    restored = SrtpEpochKeeper()
    restored.restore(saved)
    with pytest.raises(SrtpError) as exc:
        restored.unprotect(1, packet)
    assert exc.value.code == "REPLAY"
    later = restored.protect(1, 10, b"ten")
    assert restored.unprotect(1, later) == b"ten"


def test_expired_key_cannot_protect():
    keeper = SrtpEpochKeeper()
    keeper.add_stream(1)
    keeper.install_key(1, 1, KEY1, active=True, expires_at=100)
    assert keeper.protect(1, 1, b"early", now=50)["epoch"] == 1
    keeper.expire_keys(100)
    with pytest.raises(SrtpError) as exc:
        keeper.protect(1, 2, b"late", now=101)
    assert exc.value.code == "EKEYEXPIRED"


def test_control_and_media_sequences_are_independent():
    keeper = SrtpEpochKeeper()
    keeper.add_stream(1)
    keeper.install_key(1, 1, KEY1, active=True)
    control = keeper.protect(1, 0, b"control", control=True)
    media = keeper.protect(1, 0, b"media", control=False)
    assert keeper.next_seq(1, control=True) == 1
    assert keeper.next_seq(1, control=False) == 1
    assert keeper.unprotect(1, control) == b"control"
    assert keeper.unprotect(1, media) == b"media"


def test_retransmission_uses_old_epoch_key():
    keeper = SrtpEpochKeeper()
    keeper.add_stream(1)
    keeper.install_key(1, 1, KEY1, active=True)
    keeper.install_key(1, 2, KEY2)
    keeper.activate(1, 2)
    retransmit = keeper.protect(1, 7, b"old", epoch=1, retransmit=True)
    assert retransmit["epoch"] == 1
    assert keeper.unprotect(1, retransmit) == b"old"


def test_concurrent_activation_allows_only_one_new_epoch():
    keeper = SrtpEpochKeeper()
    keeper.add_stream(1)
    keeper.install_key(1, 1, KEY1, active=True)
    keeper.install_key(1, 2, KEY2)
    keeper.install_key(1, 3, KEY3)
    barrier = threading.Barrier(2)

    def worker(epoch):
        barrier.wait()
        try:
            keeper.activate(1, epoch)
        except SrtpError as exc:
            return exc.code
        return "ok"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(worker, [2, 3]))
    assert results.count("ok") == 1
    assert results.count("EBUSY") == 1
    assert keeper.active_epoch(1) in (2, 3)


def test_sequence_number_wraparound_is_accepted_once():
    keeper = SrtpEpochKeeper()
    keeper.add_stream(1)
    keeper.install_key(1, 1, KEY1, active=True)
    last = keeper.protect(1, 65535, b"last")
    wrapped = keeper.protect(1, 0, b"wrapped")
    assert keeper.unprotect(1, last) == b"last"
    assert keeper.unprotect(1, wrapped) == b"wrapped"
    with pytest.raises(SrtpError) as exc:
        keeper.unprotect(1, last)
    assert exc.value.code == "REPLAY"


def test_audit_log_never_contains_key_material():
    keeper = SrtpEpochKeeper()
    keeper.add_stream(1)
    keeper.install_key(1, 1, KEY1, active=True)
    packet = keeper.protect(1, 1, b"payload")
    keeper.unprotect(1, packet)
    text = str(keeper.audit_events())
    assert KEY1.hex() not in text
    assert KEY1.decode() not in text


def test_same_sequence_in_two_ssrc_uses_the_right_key():
    keeper = SrtpEpochKeeper()
    keeper.add_stream(101)
    keeper.add_stream(202)
    keeper.install_key(101, 1, KEY1, active=True)
    keeper.install_key(202, 1, KEY2, active=True)
    packet_a = keeper.protect(101, 1, b"alpha")
    packet_b = keeper.protect(202, 1, b"beta")
    assert keeper.unprotect(101, packet_a) == b"alpha"
    assert keeper.unprotect(202, packet_b) == b"beta"


def test_replayed_packet_is_dropped_and_counted():
    keeper = SrtpEpochKeeper()
    keeper.add_stream(1)
    keeper.install_key(1, 1, KEY1, active=True)
    packet = keeper.protect(1, 5, b"once")
    assert keeper.unprotect(1, packet) == b"once"
    with pytest.raises(SrtpError) as exc:
        keeper.unprotect(1, packet)
    assert exc.value.code == "REPLAY"
    assert keeper.replay_count(1) == 1
    assert keeper.unprotect(1, keeper.protect(1, 6, b"next")) == b"next"
    assert keeper.replay_count(1) == 1


def test_protect_without_installed_key_is_rejected():
    keeper = SrtpEpochKeeper()
    keeper.add_stream(1)
    with pytest.raises(SrtpError) as exc:
        keeper.protect(1, 1, b"nokey")
    assert exc.value.code == "EKEY"
