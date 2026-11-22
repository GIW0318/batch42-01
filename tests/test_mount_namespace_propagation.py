import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from mount_namespace_propagation import MountPolicyError, MountPropagationGuard
import mount_namespace_propagation


def test_readonly_mount_cannot_be_remounted_writable():
    guard = MountPropagationGuard()
    guard.mount("c1", "/srv/volumes/a", "/data", readonly=True)
    with pytest.raises(MountPolicyError) as exc:
        guard.remount("c1", "/data", readonly=False)
    assert exc.value.code == "EPERM"
    result = guard.remount("c1", "/data", readonly=True)
    assert result["readonly"] is True


def test_busy_umount_is_reported_and_mount_remains():
    guard = MountPropagationGuard()
    guard.mount("c1", "/srv/volumes/a", "/data")
    with pytest.raises(MountPolicyError) as exc:
        guard.umount("c1", "/data", busy=True)
    assert exc.value.code == "EBUSY"
    assert guard.mount_count() == 1
    assert guard.mounts("c1")[0]["target"] == "/data"


def test_create_container_rolls_back_on_partial_failure():
    guard = MountPropagationGuard()
    with pytest.raises(MountPolicyError) as exc:
        guard.create_container(
            "c1",
            [
                {"source": "/srv/volumes/a", "target": "/data"},
                {"source": "/srv/volumes/b", "target": "relative"},
            ],
        )
    assert exc.value.code == "EINVAL"
    assert guard.mount_count() == 0
    assert guard.mounts("c1") == []


def test_concurrent_containers_do_not_see_each_other_mounts():
    guard = MountPropagationGuard()
    barrier = threading.Barrier(2)

    def worker(container_id, prefix):
        barrier.wait()
        guard.create_container(
            container_id,
            [
                {
                    "source": "/srv/volumes/%s%d" % (prefix, index),
                    "target": "/data/%d" % index,
                }
                for index in range(10)
            ],
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(worker, "c1", "a"),
            pool.submit(worker, "c2", "b"),
        ]
        for future in futures:
            future.result()
    assert len(guard.mounts("c1")) == 10
    assert len(guard.mounts("c2")) == 10
    assert all(item["source"].startswith("/srv/volumes/a") for item in guard.mounts("c1"))
    assert all(item["source"].startswith("/srv/volumes/b") for item in guard.mounts("c2"))


def test_restart_restores_mount_table():
    guard = MountPropagationGuard()
    guard.mount("c1", "/srv/volumes/a", "/data/a", propagation="slave")
    guard.mount("c2", "/srv/volumes/b", "/data/b", readonly=True)
    restored = MountPropagationGuard()
    restored.restore(guard.snapshot())
    assert restored.mount_count() == 2
    assert restored.mounts("c1")[0]["propagation"] == "slave"
    assert restored.mounts("c2")[0]["readonly"] is True


def test_requested_propagation_mode_is_applied():
    guard = MountPropagationGuard()
    shared = guard.mount("c1", "/srv/volumes/a", "/data/a", propagation="shared")
    private = guard.mount("c1", "/srv/volumes/b", "/data/b", propagation="private")
    assert shared["propagation"] == "shared"
    assert private["propagation"] == "private"


def test_invalid_propagation_mode_is_rejected():
    guard = MountPropagationGuard()
    with pytest.raises(MountPolicyError) as exc:
        guard.mount("c1", "/srv/volumes/a", "/data", propagation="bogus")
    assert exc.value.code == "EINVAL"


def test_symlink_source_cannot_escape_allowed_roots():
    guard = MountPropagationGuard(allowed_roots=("/srv/volumes",))
    with pytest.raises(MountPolicyError) as exc:
        guard.mount(
            "c1",
            "/srv/volumes/link",
            "/data",
            realpath=lambda path: "/etc/shadow",
        )
    assert exc.value.code == "EACCES"


def test_mount_limit_is_enforced():
    guard = MountPropagationGuard(max_mounts=2)
    guard.mount("c1", "/srv/volumes/a", "/data/a")
    guard.mount("c1", "/srv/volumes/b", "/data/b")
    with pytest.raises(MountPolicyError) as exc:
        guard.mount("c1", "/srv/volumes/c", "/data/c")
    assert exc.value.code == "ENOSPC"
    guard.umount("c1", "/data/a")
    assert guard.mount("c1", "/srv/volumes/c", "/data/c")["target"] == "/data/c"


def test_remove_container_cleans_up_all_mounts():
    guard = MountPropagationGuard()
    for index in range(3):
        guard.mount("c1", "/srv/volumes/%d" % index, "/data/%d" % index)
    removed = guard.remove_container("c1")
    assert removed == 3
    assert guard.mount_count() == 0
    assert guard.mounts("c1") == []


def test_same_source_options_do_not_leak_between_containers():
    guard = MountPropagationGuard()
    first = guard.mount("c1", "/srv/volumes/shared", "/data", readonly=True)
    second = guard.mount("c2", "/srv/volumes/shared", "/data", readonly=False)
    assert first["readonly"] is True
    assert second["readonly"] is False
    assert guard.mounts("c1")[0]["readonly"] is True
    assert guard.mounts("c2")[0]["readonly"] is False


def test_user_namespace_owner_mapping_is_applied():
    guard = MountPropagationGuard(uid_map={0: 1000})
    mapped = guard.mount("c1", "/srv/volumes/a", "/data", owner_uid=0)
    assert mapped["host_uid"] == 1000
    with pytest.raises(MountPolicyError) as exc:
        guard.mount("c1", "/srv/volumes/b", "/data/b", owner_uid=5)
    assert exc.value.code == "EINVAL"


def test_mount_target_traversal_is_rejected():
    guard = MountPropagationGuard()
    with pytest.raises(MountPolicyError) as exc:
        guard.mount("c1", "/srv/volumes/a", "/data/../../etc")
    assert exc.value.code == "EINVAL"
    with pytest.raises(MountPolicyError) as exc:
        guard.mount("c1", "/srv/volumes/a", "relative/path")
    assert exc.value.code == "EINVAL"


def test_container_mounts_are_not_visible_on_host():
    guard = MountPropagationGuard()
    guard.mount("c1", "/srv/volumes/a", "/data/secret", propagation="shared")
    assert guard.host_visible() == []


def test_umount_unknown_target_does_not_remove_other_mounts():
    guard = MountPropagationGuard()
    guard.mount("c1", "/srv/volumes/a", "/data")
    with pytest.raises(MountPolicyError) as exc:
        guard.umount("c1", "/missing")
    assert exc.value.code == "ENOENT"
    assert guard.mount_count() == 1
