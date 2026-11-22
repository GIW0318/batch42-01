import json

import pytest

import netconf_candidate_lock
from netconf_candidate_lock import NetconfCandidateLock, NetconfError


def _locked(**kwargs):
    nc = NetconfCandidateLock(**kwargs)
    nc.open_session("s1")
    nc.lock("s1", "running")
    return nc


def test_lock_running_blocks_other_session_candidate_edit():
    nc = NetconfCandidateLock()
    nc.open_session("s1")
    nc.open_session("s2")
    nc.lock("s1", "running")
    with pytest.raises(NetconfError) as exc:
        nc.lock("s2", "candidate")
    assert exc.value.code == "lock-denied"
    with pytest.raises(NetconfError) as exc:
        nc.edit_candidate("s2", {"/system/hostname": "sw2"})
    assert exc.value.code == "lock-denied"


def test_edit_requires_candidate_lock():
    nc = NetconfCandidateLock()
    nc.open_session("s1")
    with pytest.raises(NetconfError) as exc:
        nc.edit_candidate("s1", {"/system/hostname": "sw1"})
    assert exc.value.code == "lock-denied"


def test_close_session_releases_locks():
    nc = NetconfCandidateLock()
    nc.open_session("s1")
    nc.open_session("s2")
    nc.lock("s1", "running")
    nc.close_session("s1")
    assert nc.lock("s2", "candidate") == ["candidate"]
    assert nc.edit_candidate("s2", {"/system/hostname": "sw2/"}) == 1


def test_unlock_enables_other_session():
    nc = NetconfCandidateLock()
    nc.open_session("s1")
    nc.open_session("s2")
    nc.lock("s1", "running")
    nc.unlock("s1", "running")
    assert nc.lock("s2", "running") == ["candidate", "running"]


def test_stale_version_commit_rejected():
    nc = _locked()
    first = nc.edit_candidate("s1", {"/system/hostname": "sw1"})
    nc.edit_candidate("s1", {"/system/domain": "corp"})
    with pytest.raises(NetconfError) as exc:
        nc.commit("s1", version=first)
    assert exc.value.code == "stale-version"
    assert nc.get_running("s1") == {}


def test_duplicate_commit_is_idempotent():
    nc = _locked()
    version = nc.edit_candidate("s1", {"/system/hostname": "sw1"})
    first = nc.commit("s1", version=version)
    second = nc.commit("s1", version=version)
    assert first["status"] == "ok"
    assert second["duplicate"] is True
    assert second["version"] == first["version"]
    assert nc.get_running("s1") == {"/system/hostname": "sw1"}


def test_commit_rolls_back_all_nodes_on_failure():
    nc = _locked()
    changes = {"/nodes/1": 1, "/nodes/2": 2, "/nodes/3": 3}
    version = nc.edit_candidate("s1", changes)
    with pytest.raises(NetconfError) as exc:
        nc.commit("s1", version=version, fail_nodes=["/nodes/3"])
    assert exc.value.code == "rollback-on-error"
    assert nc.get_running("s1") == {}
    assert nc.get_candidate("s1") == changes


def test_error_reply_lists_each_failed_node_with_error_tag():
    nc = _locked()
    version = nc.edit_candidate("s1", {"/a": 1, "/b": 2, "/c": 3})
    with pytest.raises(NetconfError) as exc:
        nc.commit("s1", version=version, fail_nodes=["/a", "/c"])
    errors = exc.value.errors
    assert [item["error-path"] for item in errors] == ["/a", "/c"]
    assert all(item["error-tag"] == "invalid-value" for item in errors)


def test_confirmed_commit_rolls_back_on_timeout():
    nc = _locked(confirmed_timeout=60)
    version = nc.edit_candidate("s1", {"/system/hostname": "sw1"})
    result = nc.commit("s1", version=version, confirmed=True)
    assert result["status"] == "pending"
    assert nc.get_running("s1") == {"/system/hostname": "sw1"}
    nc.advance(59)
    assert nc.get_running("s1") == {"/system/hostname": "sw1"}
    events = nc.advance(2)
    assert events and events[0]["status"] == "rolled-back"
    assert nc.get_running("s1") == {}


def test_confirmed_commit_pending_survives_restart():
    nc = _locked(confirmed_timeout=60)
    version = nc.edit_candidate("s1", {"/system/hostname": "sw1"})
    nc.commit("s1", version=version, confirmed=True)
    snapshot = nc.snapshot()
    restored = NetconfCandidateLock(confirmed_timeout=60)
    restored.restore(json.loads(json.dumps(snapshot)))
    assert restored.get_running() == {"/system/hostname": "sw1"}
    restored.advance(61)
    assert restored.get_running() == {}


def test_discard_keeps_defaults_out_of_candidate():
    nc = _locked(defaults={"/system/mtu": 1500})
    nc.edit_candidate("s1", {"/system/hostname": "sw1"})
    discarded = nc.discard_changes("s1")
    assert discarded == ["/system/hostname"]
    assert nc.get_candidate("s1") == {}
    assert "/system/mtu" not in nc.get_running("s1")


def test_discard_with_stale_version_keeps_candidate():
    nc = _locked()
    first = nc.edit_candidate("s1", {"/system/hostname": "sw1"})
    second = nc.edit_candidate("s1", {"/system/domain": "corp"})
    with pytest.raises(NetconfError) as exc:
        nc.discard_changes("s1", expected_version=first)
    assert exc.value.code == "stale-version"
    assert nc.get_candidate("s1") == {
        "/system/hostname": "sw1",
        "/system/domain": "corp",
    }
    assert nc.discard_changes("s1", expected_version=second) == [
        "/system/domain",
        "/system/hostname",
    ]


def test_copy_config_filters_sensitive_nodes():
    nc = NetconfCandidateLock(initial_running={
        "/system/private-key": "secret",
        "/snmp/community": "public",
        "/system/hostname": "sw1",
    })
    nc.open_session("s1")
    nc.lock("s1", "candidate")
    copied = nc.copy_config("s1", "running", "candidate")
    assert copied == {"/system/hostname": "sw1"}
    blob = json.dumps(copied)
    assert "secret" not in blob
    assert "public" not in blob


def test_stale_snapshot_from_second_session_rejected():
    nc = NetconfCandidateLock()
    nc.open_session("s1")
    nc.open_session("s2")
    base_version = 0
    nc.lock("s1", "running")
    first = nc.edit_candidate("s1", {"/system/hostname": "sw1"})
    nc.commit("s1", version=first)
    nc.close_session("s1")
    nc.lock("s2", "running")
    with pytest.raises(NetconfError) as exc:
        nc.commit("s2", version=base_version)
    assert exc.value.code == "stale-version"


def test_candidate_edits_do_not_touch_running_before_commit():
    nc = _locked()
    version = nc.edit_candidate("s1", {"/interfaces/1/enabled": True})
    assert nc.get_candidate("s1") == {"/interfaces/1/enabled": True}
    assert nc.get_running("s1") == {}
    nc.commit("s1", version=version)
    assert nc.get_running("s1") == {"/interfaces/1/enabled": True}
    assert nc.get_candidate("s1") == {}
