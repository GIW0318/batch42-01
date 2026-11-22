import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from kernel_keyring_quota import KeyringError, KeyringQuotaBroker
import kernel_keyring_quota


def test_uid_quota_exceeded_returns_edquot():
    broker = KeyringQuotaBroker(max_entries=2)
    broker.add(1000, "session", "db-pass-1", b"one")
    broker.add(1000, "session", "db-pass-2", b"two")
    with pytest.raises(KeyringError) as exc:
        broker.add(1000, "session", "db-pass-3", b"three")
    assert exc.value.code == "EDQUOT"
    assert broker.quota(1000)["entries"] == 2


def test_concurrent_same_description_keeps_one_version():
    broker = KeyringQuotaBroker()
    barrier = threading.Barrier(8)

    def worker(index):
        barrier.wait()
        try:
            broker.add(1000, "session", "shared", b"payload-%d" % index)
        except KeyringError as exc:
            return exc.code
        return "ok"

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(worker, range(8)))
    assert results.count("ok") == 1
    assert results.count("EEXIST") == 7
    assert broker.quota(1000)["entries"] == 1
    value = broker.read(1000, "session", "shared")
    assert value in {b"payload-%d" % index for index in range(8)}


def test_other_uid_cannot_read_private_key():
    broker = KeyringQuotaBroker()
    broker.add(1000, "session", "db-pass", b"secret", perms=0o600)
    with pytest.raises(KeyringError) as exc:
        broker.read(
            1000, "session", "db-pass", requester_uid=1001
        )
    assert exc.value.code == "EACCES"
    assert broker.read(1000, "session", "db-pass", requester_uid=1000) == b"secret"


def test_process_exit_reclaims_session_entries():
    broker = KeyringQuotaBroker()
    broker.add(1000, "session", "db-pass", b"secret")
    assert broker.quota(1000)["entries"] == 1
    broker.process_exit(1000, "session")
    assert broker.quota(1000)["entries"] == 0
    with pytest.raises(KeyringError) as exc:
        broker.read(1000, "session", "db-pass")
    assert exc.value.code == "ENOENT"


def test_setuid_cannot_access_old_session_keyring():
    broker = KeyringQuotaBroker()
    broker.add(1000, "session", "db-pass", b"secret")
    broker.setuid(1000, 2000)
    with pytest.raises(KeyringError) as exc:
        broker.read(
            1000, "session", "db-pass", requester_uid=2000
        )
    assert exc.value.code == "EACCES"


def test_expired_key_is_rejected():
    broker = KeyringQuotaBroker()
    broker.add(1000, "session", "db-pass", b"secret", timeout=10.0, now=0.0)
    assert broker.read(1000, "session", "db-pass", now=5.0) == b"secret"
    with pytest.raises(KeyringError) as exc:
        broker.read(1000, "session", "db-pass", now=11.0)
    assert exc.value.code == "EKEYEXPIRED"


def test_oversized_payload_is_rejected_not_truncated():
    broker = KeyringQuotaBroker(max_bytes=100)
    with pytest.raises(KeyringError) as exc:
        broker.add(1000, "session", "big", b"X" * 200)
    assert exc.value.code == "E2BIG"
    assert broker.quota(1000)["entries"] == 0


def test_restart_restores_persistent_keyring():
    broker = KeyringQuotaBroker()
    broker.add(1000, "persistent", "db-pass", b"secret")
    snapshot = broker.snapshot()
    restored = KeyringQuotaBroker()
    restored.restore(snapshot)
    assert restored.read(1000, "persistent", "db-pass") == b"secret"


def test_revoke_atomically_invalidates_references():
    broker = KeyringQuotaBroker()
    key = broker.add(1000, "session", "db-pass", b"secret")
    broker.revoke(key["serial"])
    with pytest.raises(KeyringError) as exc:
        broker.read(1000, "session", "db-pass")
    assert exc.value.code == "EKEYREVOKED"
    assert broker.quota(1000)["entries"] == 0


def test_keyring_link_cycle_is_detected():
    broker = KeyringQuotaBroker()
    broker.link_keyring(1000, "a", "b")
    broker.link_keyring(1000, "b", "a")
    with pytest.raises(KeyringError) as exc:
        broker.search(1000, "a", "anything")
    assert exc.value.code == "ELOOP"


def test_concurrent_update_is_never_torn():
    broker = KeyringQuotaBroker()
    key = broker.add(1000, "session", "config", b"version-0")
    valid = {b"version-%d" % index for index in range(40)}
    stop = threading.Event()

    def writer():
        for index in range(1, 40):
            broker.update(key["serial"], b"version-%d" % index)
        stop.set()

    def reader(_):
        seen = set()
        while not stop.is_set():
            seen.add(broker.read(1000, "session", "config"))
        return seen

    with ThreadPoolExecutor(max_workers=4) as pool:
        writer_future = pool.submit(writer)
        readers = [pool.submit(reader, index) for index in range(3)]
        writer_future.result()
        values = set()
        for future in readers:
            values.update(future.result())
    assert values <= valid


def test_audit_records_key_serial():
    broker = KeyringQuotaBroker()
    key = broker.add(1000, "session", "db-pass", b"secret")
    events = broker.audit_events()
    assert any(event["serial"] == key["serial"] for event in events)


def test_quota_is_accounted_per_uid():
    broker = KeyringQuotaBroker(max_entries=1)
    broker.add(1000, "session", "one", b"1")
    broker.add(2000, "session", "one", b"2")
    with pytest.raises(KeyringError) as exc:
        broker.add(1000, "session", "two", b"3")
    assert exc.value.code == "EDQUOT"
    assert broker.quota(2000)["entries"] == 1


def test_serial_numbers_are_unique_under_concurrency():
    broker = KeyringQuotaBroker()
    barrier = threading.Barrier(8)

    def worker(index):
        barrier.wait()
        try:
            return broker.add(
                1000, "session", "key-%d" % index, b"value-%d" % index
            )["serial"]
        except Exception:
            return ""

    with ThreadPoolExecutor(max_workers=8) as pool:
        serials = list(pool.map(worker, range(64)))
    assert len(set(serials)) == 64


def test_unlink_respects_reference_count():
    broker = KeyringQuotaBroker()
    key = broker.add(1000, "session", "db-pass", b"secret")
    assert broker.ref(key["serial"]) == 2
    assert broker.unlink(key["serial"]) == 1
    assert broker.read(1000, "session", "db-pass") == b"secret"
    assert broker.unlink(key["serial"]) == 0
    with pytest.raises(KeyringError) as exc:
        broker.read(1000, "session", "db-pass")
    assert exc.value.code == "ENOENT"
    assert broker.quota(1000)["entries"] == 0
