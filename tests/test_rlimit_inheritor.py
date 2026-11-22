import json
import threading

import pytest

import rlimit_inheritor
from rlimit_inheritor import RlimitError, RlimitInheritor

MAX_VALUES = {"NOFILE": 2 ** 40, "NPROC": 2 ** 20, "CPU": 2 ** 40}


def _inheritor(**kwargs):
    options = dict(max_values=MAX_VALUES, cpu_grace=5)
    options.update(kwargs)
    return RlimitInheritor(**options)


def test_fork_inherits_both_limits():
    inheritor = _inheritor()
    inheritor.set_limit("init", "NOFILE", 512, 2048)
    inheritor.fork("init", "child")
    child = inheritor.get_limit("child", "NOFILE")
    assert child == {"soft": 512, "hard": 2048}


def test_raising_hard_limit_requires_privilege():
    inheritor = _inheritor()
    with pytest.raises(RlimitError) as exc:
        inheritor.set_limit("init", "NOFILE", 1024, 5000)
    assert exc.value.code == "permission-denied"
    assert inheritor.get_limit("init", "NOFILE") == {"soft": 1024, "hard": 4096}


def test_privileged_can_raise_hard_limit():
    inheritor = _inheritor(privileged=True)
    inheritor.set_limit("init", "NOFILE", 1024, 8192)
    assert inheritor.get_limit("init", "NOFILE") == {"soft": 1024, "hard": 8192}


def test_soft_above_hard_rejected():
    inheritor = _inheritor()
    with pytest.raises(RlimitError) as exc:
        inheritor.set_limit("init", "NOFILE", 5000, 1000)
    assert exc.value.code == "invalid-limit"
    assert inheritor.get_limit("init", "NOFILE") == {"soft": 1024, "hard": 4096}


def test_lower_nofile_new_fd_emfile():
    inheritor = _inheritor()
    inheritor.set_limit("init", "NOFILE", 2, 2)
    inheritor.open_fd("init", 2)
    with pytest.raises(RlimitError) as exc:
        inheritor.open_fd("init", 1)
    assert exc.value.code == "EMFILE"
    assert inheritor.open_fd_count("init") == 2


def test_existing_fds_survive_lowering():
    inheritor = _inheritor()
    inheritor.set_limit("init", "NOFILE", 10, 10)
    inheritor.open_fd("init", 3)
    inheritor.set_limit("init", "NOFILE", 1, 10)
    assert inheritor.open_fd_count("init") == 3
    with pytest.raises(RlimitError) as exc:
        inheritor.open_fd("init", 1)
    assert exc.value.code == "EMFILE"


def test_unit_limit_ceiling_enforced():
    inheritor = _inheritor()
    inheritor.set_unit_limit("NOFILE", 64, 64)
    inheritor.set_limit("init", "NOFILE", 32, 32)
    assert inheritor.get_limit("init", "NOFILE") == {"soft": 32, "hard": 32}
    with pytest.raises(RlimitError) as exc:
        inheritor.set_limit("init", "NOFILE", 128, 128)
    assert exc.value.code == "unit-limit"
    assert inheritor.get_limit("init", "NOFILE") == {"soft": 32, "hard": 32}


def test_cpu_grace_before_kill():
    inheritor = _inheritor(cpu_grace=5)
    signal = inheritor.cpu_signal("init")
    assert signal["signal"] == "SIGXCPU"
    assert inheritor.is_killed("init") is False
    inheritor.advance(4)
    assert inheritor.is_killed("init") is False
    inheritor.advance(2)
    assert inheritor.is_killed("init") is True


def test_process_count_per_namespace():
    inheritor = _inheritor()
    inheritor.register_process("p1", "1000", namespace="container-a")
    inheritor.register_process("p2", "1000", namespace="container-b")
    assert inheritor.process_count("1000", namespace="container-a") == 1
    assert inheritor.process_count("1000", namespace="container-b") == 1
    assert inheritor.process_count("1000", namespace="host") == 0


def test_value_64bit_no_overflow():
    inheritor = _inheritor(privileged=True)
    inheritor.set_limit("init", "NOFILE", 2 ** 32, 2 ** 32)
    limit = inheritor.get_limit("init", "NOFILE")
    assert limit["soft"] == 2 ** 32
    assert limit["soft"] > 0
    with pytest.raises(RlimitError) as exc:
        inheritor.set_limit("init", "NOFILE", 2 ** 41, 2 ** 41)
    assert exc.value.code == "invalid-limit"


def test_invalid_resource_rejected():
    inheritor = _inheritor()
    with pytest.raises(RlimitError) as exc:
        inheritor.set_limit("init", "BOGUS", 1, 1)
    assert exc.value.code == "unknown-resource"


def test_restore_usage_recheck():
    inheritor = _inheritor()
    inheritor.set_limit("init", "NOFILE", 5, 5)
    inheritor.open_fd("init", 2)
    snapshot = inheritor.snapshot()
    restored = _inheritor()
    restored.restore(json.loads(json.dumps(snapshot)))
    assert restored.open_fd_count("init") == 2
    with pytest.raises(RlimitError) as exc:
        restored.open_fd("init", 4)
    assert exc.value.code == "EMFILE"


def test_exec_preserves_hard_limit():
    inheritor = _inheritor()
    inheritor.set_limit("init", "NOFILE", 512, 2048)
    inheritor.exec_process("init", "exec-child")
    assert inheritor.get_limit("exec-child", "NOFILE") == {"soft": 512, "hard": 2048}


def test_multithread_visibility_after_set():
    inheritor = _inheritor()
    barrier = threading.Barrier(2)
    errors = []

    def worker(soft):
        barrier.wait()
        try:
            inheritor.set_limit("init", "NOFILE", soft, 4096)
        except Exception as exc:
            errors.append(getattr(exc, "code", type(exc).__name__))

    threads = [
        threading.Thread(target=worker, args=(100,)),
        threading.Thread(target=worker, args=(200,)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    limit = inheritor.get_limit("init", "NOFILE")
    assert limit["soft"] in (100, 200)
    assert limit["hard"] == 4096


def test_snapshot_restore_limits():
    inheritor = _inheritor()
    inheritor.set_limit("init", "NOFILE", 512, 2048)
    snapshot = inheritor.snapshot()
    restored = _inheritor()
    restored.restore(snapshot)
    assert restored.get_limit("init", "NOFILE") == {"soft": 512, "hard": 2048}
