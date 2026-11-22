import os
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from core_dump_filter import CoreDumpPolicy, CoreDumpError
import core_dump_filter


def process(**overrides):
    value = {
        "pid": 4242,
        "name": "worker",
        "path": "/usr/bin/worker",
        "sensitive": False,
        "secret_markers": [],
        "groups": [],
    }
    value.update(overrides)
    return value


def test_sensitive_process_dump_filters_secret_bytes(tmp_path):
    policy = CoreDumpPolicy(core_dir=str(tmp_path))
    secret = b"TOP-SECRET-KEY-MATERIAL"
    record = policy.dump(
        process(sensitive=True, secret_markers=[secret]),
        b"prefix " + secret + b" suffix",
    )
    data = policy.read_dump(record["path"])
    assert secret not in data
    assert record["redactions"] >= 1
    assert b"prefix" in data


def test_oversized_dump_is_skipped(tmp_path):
    policy = CoreDumpPolicy(core_dir=str(tmp_path), max_bytes=1024)
    record = policy.dump(process(), b"X" * 2048)
    assert record["status"] == "skipped_oversize"
    assert record.get("path") is None
    assert os.listdir(tmp_path) == []


def test_concurrent_same_name_dumps_get_unique_paths(tmp_path):
    policy = CoreDumpPolicy(core_dir=str(tmp_path))
    barrier = threading.Barrier(8)

    def worker(index):
        barrier.wait()
        try:
            return policy.dump(
                process(pid=100, name="same-name"), b"memory-%d" % index
            )["path"]
        except Exception:
            return ""

    with ThreadPoolExecutor(max_workers=8) as pool:
        paths = list(pool.map(worker, range(24)))
    assert len(set(paths)) == 24
    assert all(os.path.exists(path) for path in paths)


def test_only_one_handler_is_active(tmp_path):
    policy = CoreDumpPolicy(core_dir=str(tmp_path))
    policy.set_handler("systemd")
    policy.set_handler("custom")
    assert policy.handlers() == ["custom"]
    record = policy.dump(process(), b"memory")
    assert record["handled_by"] == "custom"
    assert record["writes"] == 1


def test_handler_failure_falls_back_to_safe_path(tmp_path):
    policy = CoreDumpPolicy(core_dir=str(tmp_path))
    result = policy.handle_failure(process())
    assert result["status"] == "fallback_safe"
    assert result["path"]
    assert result["error"]


def test_handler_drops_inherited_supplementary_groups(tmp_path):
    policy = CoreDumpPolicy(core_dir=str(tmp_path))
    record = policy.dump(
        process(groups=["shadow", "docker", "adm"]), b"memory"
    )
    assert record["handler_groups"] == []


def test_dump_reports_owner_only_permissions(tmp_path):
    policy = CoreDumpPolicy(core_dir=str(tmp_path))
    record = policy.dump(process(), b"memory")
    assert record["mode"] == 0o600


def test_cleanup_removes_stale_cores(tmp_path):
    policy = CoreDumpPolicy(core_dir=str(tmp_path))
    record = policy.dump(process(), b"memory", now=0.0)
    removed = policy.cleanup(now=100000.0, max_age=3600.0)
    assert record["path"] in removed
    assert not os.path.exists(record["path"])


def test_process_name_only_match_does_not_bypass_path_policy():
    policy = CoreDumpPolicy(
        sensitive_paths=["/usr/sbin/nginx"]
    )
    real = policy.decide(process(name="nginx", path="/usr/sbin/nginx"))
    impostor = policy.decide(process(name="nginx", path="/tmp/nginx"))
    assert real["filter"] is True
    assert impostor["filter"] is False


def test_symlinked_output_path_is_rejected(tmp_path):
    policy = CoreDumpPolicy(core_dir=str(tmp_path))
    with pytest.raises(CoreDumpError) as exc:
        policy.dump(
            process(), b"memory", realpath=lambda path: "/etc/passwd"
        )
    assert exc.value.code == "ELOOP"


def test_concurrent_dumps_respect_concurrency_limit(tmp_path):
    policy = CoreDumpPolicy(core_dir=str(tmp_path), max_concurrent=2)
    barrier = threading.Barrier(6)

    def worker(index):
        barrier.wait()
        return policy.dump(process(pid=index), b"M" * 100000)["status"]

    with ThreadPoolExecutor(max_workers=6) as pool:
        statuses = list(pool.map(worker, range(6)))
    assert statuses == ["ok"] * 6


def test_audit_records_dump_event(tmp_path):
    policy = CoreDumpPolicy(core_dir=str(tmp_path))
    record = policy.dump(process(), b"memory", now=10.0)
    events = policy.audit_events()
    assert events
    event = events[-1]
    assert event["pid"] == 4242
    assert event["path"] == record["path"]
    assert event["bytes"] == 6
    assert "redactions" in event


def test_non_sensitive_dump_preserves_memory(tmp_path):
    policy = CoreDumpPolicy(core_dir=str(tmp_path))
    payload = b"NOT-A-SECRET-PLAINTEXT"
    record = policy.dump(process(), payload)
    assert policy.read_dump(record["path"]) == payload


def test_decide_marks_sensitive_path_for_filtering():
    policy = CoreDumpPolicy(sensitive_paths=["/usr/bin/worker"])
    decision = policy.decide(process())
    assert decision["dump"] is True
    assert decision["filter"] is True


def test_cleanup_keeps_recent_cores(tmp_path):
    policy = CoreDumpPolicy(core_dir=str(tmp_path))
    record = policy.dump(process(), b"memory", now=100000.0)
    removed = policy.cleanup(now=100100.0, max_age=3600.0)
    assert removed == []
    assert os.path.exists(record["path"])
