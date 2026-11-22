import threading

import json
import pytest

import sysctl_scope_writer
from sysctl_scope_writer import SysctlError, SysctlScopeWriter

PARAMS = {
    "net.ipv4.ip_forward": {
        "namespaced": True, "type": "int", "min": 0, "max": 1,
        "default": "0", "writable": True,
        "depends_on": ["net.netfilter.nf_conntrack_max"],
    },
    "net.netfilter.nf_conntrack_max": {
        "namespaced": True, "type": "int", "min": 1, "max": 2 ** 31 - 1,
        "default": "65536", "writable": True,
    },
    "kernel.pid_max": {
        "namespaced": False, "type": "int", "min": 1, "max": 2 ** 31 - 1,
        "default": "32768", "writable": True,
    },
    "kernel.randomize_va_space": {
        "namespaced": False, "type": "int", "min": 0, "max": 2,
        "default": "2", "writable": False,
    },
    "net.ipv4.tcp_deprecated": {
        "namespaced": True, "type": "int", "min": 0, "max": 10,
        "default": "1", "writable": True, "deprecated": True,
    },
    "test.big_limit": {
        "namespaced": True, "type": "int", "min": 0, "max": 2 ** 40,
        "default": "0", "writable": True,
    },
}
CONTAINER = "container:abc"


def _writer(params=None):
    return SysctlScopeWriter(params=params or PARAMS)


def test_namespaced_write_isolated():
    writer = _writer()
    writer.set(CONTAINER, "net.ipv4.ip_forward", "1")
    assert writer.get(CONTAINER, "net.ipv4.ip_forward") == "1"
    assert writer.get("host", "net.ipv4.ip_forward") == "0"


def test_non_namespaced_rejected_in_container():
    writer = _writer()
    with pytest.raises(SysctlError) as exc:
        writer.set(CONTAINER, "kernel.pid_max", "100000")
    assert exc.value.code == "not-namespaced"
    assert writer.get(CONTAINER, "kernel.pid_max") is None


def test_read_only_rejected():
    writer = _writer()
    with pytest.raises(SysctlError) as exc:
        writer.set("host", "kernel.randomize_va_space", "0")
    assert exc.value.code == "read-only"


def test_deprecated_parameter_rejected():
    writer = _writer()
    with pytest.raises(SysctlError) as exc:
        writer.set("host", "net.ipv4.tcp_deprecated", "2")
    assert exc.value.code == "deprecated-parameter"


def test_invalid_value_rejected():
    writer = _writer()
    with pytest.raises(SysctlError) as exc:
        writer.set("host", "net.ipv4.ip_forward", "yes")
    assert exc.value.code == "invalid-value"
    assert writer.get("host", "net.ipv4.ip_forward") == "0"


def test_whitespace_normalized():
    writer = _writer()
    writer.set("host", "net.ipv4.ip_forward", " 1 ")
    assert writer.get("host", "net.ipv4.ip_forward") == "1"


def test_range_64bit_no_wrap():
    writer = _writer()
    writer.set(CONTAINER, "test.big_limit", str(2 ** 40))
    assert writer.get(CONTAINER, "test.big_limit") == str(2 ** 40)
    with pytest.raises(SysctlError) as exc:
        writer.set(CONTAINER, "test.big_limit", str(2 ** 40 + 1))
    assert exc.value.code == "out-of-range"


def test_batch_atomic_rollback():
    writer = _writer()
    assignments = [
        ("net.netfilter.nf_conntrack_max", "131072"),
        ("net.ipv4.ip_forward", "1"),
        ("test.big_limit", "4096"),
        ("kernel.pid_max", "100000"),
        ("net.ipv4.ip_forward", "not-a-number"),
    ]
    with pytest.raises(SysctlError) as exc:
        writer.set_many(CONTAINER, assignments)
    assert exc.value.code == "not-namespaced"
    assert writer.get(CONTAINER, "net.netfilter.nf_conntrack_max") is None
    assert writer.get(CONTAINER, "test.big_limit") is None


def test_dependency_order_enforced():
    writer = _writer()
    with pytest.raises(SysctlError) as exc:
        writer.set_many(CONTAINER, [
            ("net.ipv4.ip_forward", "1"),
            ("net.netfilter.nf_conntrack_max", "131072"),
        ])
    assert exc.value.code == "dependency-order"
    writer.set_many(CONTAINER, [
        ("net.netfilter.nf_conntrack_max", "131072"),
        ("net.ipv4.ip_forward", "1"),
    ])
    assert writer.get(CONTAINER, "net.ipv4.ip_forward") == "1"


def test_concurrent_same_namespace_serialized():
    writer = _writer()
    barrier = threading.Barrier(2)
    errors = []

    def worker(value):
        barrier.wait()
        try:
            writer.set(CONTAINER, "test.big_limit", value)
        except Exception as exc:
            errors.append(getattr(exc, "code", type(exc).__name__))

    threads = [
        threading.Thread(target=worker, args=("10",)),
        threading.Thread(target=worker, args=("20",)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    assert writer.get(CONTAINER, "test.big_limit") in ("10", "20")
    entries = [item for item in writer.audit_log()
               if item["name"] == "test.big_limit"]
    assert len(entries) == 2


def test_namespace_read_isolation():
    writer = _writer()
    writer.set("host", "net.ipv4.ip_forward", "1")
    writer.set(CONTAINER, "net.ipv4.ip_forward", "0")
    assert writer.get("host", "net.ipv4.ip_forward") == "1"
    assert writer.get(CONTAINER, "net.ipv4.ip_forward") == "0"


def test_restart_restores_persisted_values():
    writer = _writer()
    writer.set(CONTAINER, "net.netfilter.nf_conntrack_max", "131072")
    writer.set(CONTAINER, "net.ipv4.ip_forward", "1")
    snapshot = writer.snapshot()
    restored = _writer()
    restored.restore(json.loads(json.dumps(snapshot)))
    assert restored.get(CONTAINER, "net.ipv4.ip_forward") == "1"
    assert restored.get("host", "net.ipv4.ip_forward") == "0"


def test_audit_includes_namespace():
    writer = _writer()
    writer.set(CONTAINER, "test.big_limit", "10")
    entry = writer.audit_log()[-1]
    assert entry["namespace"] == CONTAINER
    assert entry["name"] == "test.big_limit"
    assert entry["value"] == "10"


def test_batch_success_applies_all():
    writer = _writer()
    result = writer.set_many("host", [
        ("net.netfilter.nf_conntrack_max", "131072"),
        ("net.ipv4.ip_forward", "1"),
    ])
    assert result["applied"] == 2
    assert writer.get("host", "net.ipv4.ip_forward") == "1"


def test_invalid_scope_rejected():
    writer = _writer()
    with pytest.raises(SysctlError) as exc:
        writer.set("bogus scope!", "net.ipv4.ip_forward", "1")
    assert exc.value.code == "invalid-scope"
