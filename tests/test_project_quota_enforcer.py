import threading

import json
import pytest

import project_quota_enforcer
from project_quota_enforcer import ProjectQuotaEnforcer, QuotaError


def _enforcer(**kwargs):
    enforcer = ProjectQuotaEnforcer(grace_seconds=50, **kwargs)
    enforcer.create_project("p1", block_limit=100, inode_limit=10)
    return enforcer


def test_project_usage_isolated():
    enforcer = _enforcer()
    enforcer.create_project("p2", block_limit=100, inode_limit=10)
    enforcer.reserve("p1", blocks=90, inodes=5)
    enforcer.reserve("p2", blocks=90, inodes=5)
    assert enforcer.usage("p1") == {"blocks": 90, "inodes": 5}
    assert enforcer.usage("p2") == {"blocks": 90, "inodes": 5}


def test_block_limit_enforced():
    enforcer = _enforcer()
    with pytest.raises(QuotaError) as exc:
        enforcer.reserve("p1", blocks=101)
    assert exc.value.code == "quota-exceeded"
    assert enforcer.usage("p1")["blocks"] == 0


def test_inode_limit_enforced():
    enforcer = _enforcer()
    with pytest.raises(QuotaError) as exc:
        enforcer.reserve("p1", inodes=11)
    assert exc.value.code == "quota-exceeded"
    assert enforcer.usage("p1")["inodes"] == 0


def test_concurrent_reserve_no_oversell():
    enforcer = _enforcer()
    barrier = threading.Barrier(2)
    results = []
    lock = threading.Lock()

    def worker():
        barrier.wait()
        try:
            enforcer.reserve("p1", blocks=60)
            with lock:
                results.append("ok")
        except QuotaError as exc:
            with lock:
                results.append(exc.code)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    # sorted() orders plain strings lexicographically, so "ok" comes first.
    assert sorted(results) == ["ok", "quota-exceeded"]
    assert enforcer.usage("p1")["blocks"] == 60


def test_grace_starts_at_first_exceed():
    enforcer = _enforcer()
    enforcer.advance(100)
    enforcer.reserve("p1", blocks=150)
    enforcer.advance(49)
    enforcer.reserve("p1", blocks=1)
    enforcer.advance(1)
    with pytest.raises(QuotaError) as exc:
        enforcer.reserve("p1", blocks=1)
    assert exc.value.code == "quota-exceeded"


def test_child_limited_by_parent_minimum():
    enforcer = _enforcer()
    enforcer.create_project("child", parent="p1", block_limit=200,
                            inode_limit=10)
    with pytest.raises(QuotaError) as exc:
        enforcer.reserve("child", blocks=150)
    assert exc.value.code == "quota-exceeded"
    assert enforcer.usage("child")["blocks"] == 0


def test_hardlink_inode_counted_once():
    enforcer = _enforcer()
    enforcer.reserve("p1", inodes=1, inode_id="inode-1")
    enforcer.reserve("p1", inodes=1, inode_id="inode-1")
    assert enforcer.usage("p1")["inodes"] == 1
    enforcer.reserve("p1", inodes=1, inode_id="inode-2")
    assert enforcer.usage("p1")["inodes"] == 2


def test_symlink_not_counted():
    enforcer = _enforcer()
    enforcer.reserve("p1", blocks=10, inodes=5, kind="symlink")
    assert enforcer.usage("p1") == {"blocks": 0, "inodes": 0}


def test_sparse_uses_physical_blocks():
    enforcer = _enforcer()
    enforcer.reserve("p1", blocks=10, apparent_blocks=1000, sparse=True)
    assert enforcer.usage("p1")["blocks"] == 10


def test_release_reclaims_immediately():
    enforcer = _enforcer()
    enforcer.reserve("p1", blocks=90)
    enforcer.release("p1", blocks=90)
    assert enforcer.usage("p1")["blocks"] == 0
    enforcer.reserve("p1", blocks=90)


def test_rename_checks_destination_quota():
    enforcer = _enforcer()
    enforcer.create_project("p2", block_limit=10, inode_limit=10)
    enforcer.reserve("p2", blocks=10)
    with pytest.raises(QuotaError) as exc:
        enforcer.rename("p1", "p2", "file.bin", blocks=5)
    assert exc.value.code == "quota-exceeded"
    assert enforcer.usage("p2")["blocks"] == 10


def test_truncate_growth_checked():
    enforcer = _enforcer()
    enforcer.reserve("p1", blocks=5)
    with pytest.raises(QuotaError) as exc:
        enforcer.truncate("p1", "file.bin", new_blocks=101)
    assert exc.value.code == "quota-exceeded"
    assert enforcer.usage("p1")["blocks"] == 5
    enforcer.truncate("p1", "file.bin", new_blocks=8)
    assert enforcer.usage("p1")["blocks"] == 8


def test_quota_change_applies_to_open_handle():
    enforcer = _enforcer()
    handle = enforcer.open_file("p1", "file.bin")
    enforcer.set_quota("p1", block_limit=10)
    with pytest.raises(QuotaError) as exc:
        enforcer.write(handle, blocks=20)
    assert exc.value.code == "quota-exceeded"
    assert enforcer.usage("p1")["blocks"] == 0


def test_snapshot_paths_counted():
    enforcer = _enforcer()
    enforcer.reserve("p1", blocks=10, path="/p1/.snapshot/snap-1")
    assert enforcer.usage("p1")["blocks"] == 10


def test_restart_restores_usage_and_limits():
    enforcer = _enforcer()
    enforcer.reserve("p1", blocks=40)
    snapshot = enforcer.snapshot()
    restored = ProjectQuotaEnforcer(grace_seconds=50)
    restored.restore(json.loads(json.dumps(snapshot)))
    assert restored.usage("p1")["blocks"] == 40
    with pytest.raises(QuotaError) as exc:
        restored.reserve("p1", blocks=61)
    assert exc.value.code == "quota-exceeded"
