import threading

import pytest

import fsverity_tree_checker
from fsverity_tree_checker import FsverityError, FsverityTreeChecker

CHAIN = [{"subject": "CN=Root", "issuer": "CN=Root",
          "signature_valid": True, "trusted": True}]


def _checker(**kwargs):
    return FsverityTreeChecker(**kwargs)


def _enabled(checker, inode, data, inode_name="file.bin"):
    tree = checker.build_tree(data)
    signature = checker.sign(tree, "CN=Signer", CHAIN)
    checker.enable(inode, data, signature, CHAIN)
    return signature


def test_unsupported_algorithm_rejected():
    checker = _checker()
    with pytest.raises(FsverityError) as exc:
        checker.build_tree(b"data", algorithm="md5")
    assert exc.value.code == "unsupported-algorithm"


def test_weak_signature_algorithm_rejected():
    checker = _checker()
    tree = checker.build_tree(b"data")
    signature = dict(checker.sign(tree, "CN=Signer", CHAIN))
    signature["algorithm"] = "md5"
    with pytest.raises(FsverityError) as exc:
        checker.enable("ino-1", b"data", signature, CHAIN)
    assert exc.value.code == "unsupported-algorithm"


def test_untrusted_chain_rejected():
    checker = _checker()
    tree = checker.build_tree(b"data")
    chain = [{"subject": "CN=Evil", "issuer": "CN=Evil",
              "signature_valid": True, "trusted": False}]
    with pytest.raises(FsverityError) as exc:
        checker.sign(tree, "CN=Evil", chain)
    assert exc.value.code == "untrusted-certificate"


def test_signature_binds_root():
    checker = _checker()
    tree_a = checker.build_tree(b"A" * 4096)
    signature = checker.sign(tree_a, "CN=Signer", CHAIN)
    with pytest.raises(FsverityError) as exc:
        checker.enable("ino-1", b"B" * 4096, signature, CHAIN)
    assert exc.value.code == "signature-mismatch"


def test_tail_padding_changes_root():
    checker = _checker()
    first = checker.build_tree(b"A" * 4095)
    second = checker.build_tree(b"A" * 4094 + b"B")
    assert first["root"] != second["root"]


def test_tree_levels_cover_bottom_blocks():
    checker = _checker()
    data = bytearray(b"A" * (4096 * 3))
    tree = checker.build_tree(bytes(data))
    data[-1] = ord("B")
    tampered = checker.build_tree(bytes(data))
    assert tree["root"] != tampered["root"]
    big = checker.build_tree(b"A" * (4096 * 130))
    assert big["levels"] >= 2


def test_verify_detects_same_length_tamper():
    checker = _checker()
    data = b"A" * 4096
    _enabled(checker, "ino-1", data)
    tampered = b"B" + data[1:]
    assert len(tampered) == len(data)
    assert checker.verify("ino-1", tampered) is False


def test_write_after_enable_rejected():
    checker = _checker()
    _enabled(checker, "ino-1", b"data")
    with pytest.raises(FsverityError) as exc:
        checker.write("ino-1", b"new")
    assert exc.value.code == "read-only"


def test_verify_detects_tamper():
    checker = _checker()
    data = b"trusted-content" * 300
    _enabled(checker, "ino-1", data)
    assert checker.verify("ino-1", data) is True
    assert checker.verify("ino-1", data + b"x") is False


def test_build_failure_leaves_disabled():
    checker = _checker()
    tree = checker.build_tree(b"data")
    signature = checker.sign(tree, "CN=Signer", CHAIN)
    with pytest.raises(FsverityError) as exc:
        checker.enable("ino-1", b"data", signature, CHAIN, fail_at="sign")
    assert exc.value.code == "operation-failed"
    assert checker.is_enabled("ino-1") is False
    assert checker.write("ino-1", b"allowed") == 7


def test_rename_preserves_verity():
    checker = _checker()
    data = b"rename-me" * 100
    _enabled(checker, "ino-1", data)
    assert checker.rename("ino-1", "renamed.bin") is True
    assert checker.is_enabled("ino-1") is True
    assert checker.read("ino-1") == data


def test_stale_descriptor_version_rejected():
    checker = _checker()
    _enabled(checker, "ino-1", b"data" * 100)
    version = checker.current_version("ino-1")
    assert checker.read("ino-1", version=version)
    with pytest.raises(FsverityError) as exc:
        checker.read("ino-1", version=version - 1)
    assert exc.value.code == "stale-version"


def test_io_error_vs_argument_error():
    checker = _checker()
    _enabled(checker, "ino-1", b"data")
    with pytest.raises(FsverityError) as exc:
        checker.read("missing-inode")
    assert exc.value.code == "io-error"
    with pytest.raises(FsverityError) as exc:
        checker.read("ino-1", offset=-1)
    assert exc.value.code == "invalid-argument"


def test_restart_rechecks_inode_state():
    checker = _checker()
    data = b"persisted" * 500
    _enabled(checker, "ino-1", data)
    snapshot = checker.snapshot()
    restored = _checker()
    restored.restore(snapshot)
    assert restored.is_enabled("ino-1") is True
    assert restored.verify("ino-1", data) is True
    assert restored.read("ino-1") == data


def test_concurrent_reads_consistent():
    checker = _checker()
    data = b"concurrent-content" * 400
    _enabled(checker, "ino-1", data)
    barrier = threading.Barrier(2)
    results = []
    lock = threading.Lock()

    def worker():
        barrier.wait()
        chunk = checker.read("ino-1")
        with lock:
            results.append(chunk)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert results == [data, data]
