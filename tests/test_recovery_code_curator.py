import json
import threading

import pytest

import recovery_code_curator
from recovery_code_curator import RecoveryCodeCurator, RecoveryCodeError


def _curator(**kwargs):
    return RecoveryCodeCurator(salt=b"recovery-salt", **kwargs)


def test_generated_code_shape():
    curator = _curator()
    codes = curator.generate("alice")
    assert len(codes) == 10
    assert len(set(codes)) == 10
    assert all(len(code) == 10 for code in codes)
    assert all(set(code) <= set(curator.alphabet) for code in codes)


def test_snapshot_contains_no_plaintext_codes():
    curator = _curator()
    codes = curator.generate("alice")
    blob = json.dumps(curator.snapshot())
    assert all(code not in blob for code in codes)


def test_digest_salted_across_accounts():
    curator = _curator()
    first = curator.digest_code("alice", "ABCDE12345")
    second = curator.digest_code("bob", "ABCDE12345")
    assert first != second
    assert "ABCDE12345" not in first


def test_verify_correct_and_wrong_code():
    curator = _curator()
    codes = curator.generate("alice")
    assert curator.verify("alice", codes[0]) is True
    assert curator.verify("alice", "ZZZZZZZZZZ") is False


def test_used_code_cannot_be_reused():
    curator = _curator()
    codes = curator.generate("alice")
    assert curator.consume("alice", codes[0]) is True
    assert curator.consume("alice", codes[0]) is False
    assert curator.remaining("alice") == 9


def test_normalize_case_and_hyphen():
    curator = _curator()
    codes = curator.generate("alice")
    spaced = codes[0][:5].lower() + "-" + codes[0][5:].lower()
    assert curator.normalize_code(spaced) == codes[0]
    assert curator.consume("alice", spaced) is True


def test_regenerate_replaces_all_old_codes():
    curator = _curator()
    old_codes = curator.generate("alice")
    new_codes = curator.regenerate("alice")
    assert len(new_codes) == 10
    assert all(curator.consume("alice", code) is False for code in old_codes)
    assert curator.consume("alice", new_codes[0]) is True


def test_remaining_and_low_space_warning():
    curator = _curator(code_count=3, low_threshold=2)
    codes = curator.generate("alice")
    assert curator.remaining("alice") == 3
    assert curator.warnings("alice") == []
    curator.consume("alice", codes[0])
    assert curator.remaining("alice") == 2
    assert "low-code-space" in curator.warnings("alice")


def test_disabled_account_cannot_consume():
    curator = _curator()
    codes = curator.generate("alice")
    curator.set_account_state("alice", "disabled")
    assert curator.consume("alice", codes[0]) is False
    assert curator.remaining("alice") == 10


def test_concurrent_consume_only_one():
    curator = _curator()
    codes = curator.generate("alice")
    barrier = threading.Barrier(2)
    results = []
    lock = threading.Lock()

    def worker():
        barrier.wait()
        try:
            outcome = curator.consume("alice", codes[0])
            with lock:
                results.append(outcome)
        except Exception as exc:
            with lock:
                results.append(type(exc).__name__)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(results) == [False, True]
    assert curator.remaining("alice") == 9


def test_failure_counters_are_independent():
    curator = _curator()
    curator.record_failure("alice", "recovery")
    curator.record_failure("alice", "recovery")
    curator.record_failure("alice", "otp")
    assert curator.failed_attempts("alice", "recovery") == 2
    assert curator.failed_attempts("alice", "otp") == 1


def test_snapshot_restore_keeps_used_state():
    curator = _curator()
    codes = curator.generate("alice")
    curator.consume("alice", codes[0])
    snapshot = curator.snapshot()
    restored = _curator()
    restored.restore(json.loads(json.dumps(snapshot)))
    assert restored.consume("alice", codes[0]) is False
    assert restored.remaining("alice") == 9
    assert restored.consume("alice", codes[1]) is True


def test_uniform_failure_result():
    curator = _curator(code_count=2)
    codes = curator.generate("alice")
    curator.consume("alice", codes[0])
    curator.set_account_state("bob", "disabled")
    bob_codes = curator.generate("bob")
    assert curator.consume("alice", codes[0]) is False
    assert curator.consume("alice", "NOTACODE22") is False
    assert curator.consume("bob", bob_codes[0]) is False


def test_regenerate_returns_same_quantity():
    curator = _curator(code_count=4)
    curator.generate("alice")
    replacement = curator.regenerate("alice")
    assert len(replacement) == 4
    assert curator.remaining("alice") == 4
    assert all(len(code) == 10 for code in replacement)


def test_verify_does_not_consume():
    curator = _curator()
    codes = curator.generate("alice")
    assert curator.verify("alice", codes[0]) is True
    assert curator.remaining("alice") == 10
    assert curator.consume("alice", codes[0]) is True
