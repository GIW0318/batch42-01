import pytest

import pim_join_registry
from pim_join_registry import PimError, PimJoinRegistry, PimParseError


def test_parse_valid_join_prune():
    reg = PimJoinRegistry()
    payload = bytes([239, 1, 1, 1, 2, 180, 10, 0, 0, 1, 10, 0, 0, 2])
    parsed = reg.parse_join_prune(payload)
    assert parsed["group"] == "239.1.1.1"
    assert parsed["holdtime"] == 180
    assert parsed["sources"] == ["10.0.0.1", "10.0.0.2"]


def test_parse_overrun_source_count_raises_with_field():
    reg = PimJoinRegistry()
    payload = bytes([239, 1, 1, 1, 5, 180, 10, 0, 0, 1])
    with pytest.raises(PimParseError) as exc:
        reg.parse_join_prune(payload)
    assert exc.value.field == "source_count"


def test_parse_group_address_is_unsigned():
    reg = PimJoinRegistry()
    parsed = reg.parse_join_prune(bytes([255, 0, 0, 1, 0, 180]))
    assert parsed["group"] == "255.0.0.1"
    assert parsed["group"].split(".")[0] == "255"


def test_duplicate_join_same_interface_deduped():
    reg = PimJoinRegistry()
    reg.add_join("239.1.1.1", None, "eth0", holdtime=180)
    reg.add_join("239.1.1.1", None, "eth0", holdtime=180)
    assert reg.outgoing("239.1.1.1") == ["eth0"]
    assert reg.entry_count() == 1


def test_duplicate_join_different_interface_adds():
    reg = PimJoinRegistry()
    reg.add_join("239.1.1.1", None, "eth0")
    reg.add_join("239.1.1.1", None, "eth1")
    assert reg.outgoing("239.1.1.1") == ["eth0", "eth1"]


def test_holdtime_expiry_removes_only_expired():
    reg = PimJoinRegistry()
    reg.add_join("239.1.1.1", None, "eth0", holdtime=10)
    reg.add_join("239.1.2.2", None, "eth0", holdtime=100)
    reg.advance(11)
    expired = reg.expire()
    assert [item["group"] for item in expired] == ["239.1.1.1"]
    assert reg.entry_count() == 1
    assert reg.outgoing("239.1.1.1") == []
    assert reg.outgoing("239.1.2.2") == ["eth0"]


def test_table_limit_atomic():
    reg = PimJoinRegistry(max_entries=1)
    reg.add_join("239.1.1.1", None, "eth0")
    with pytest.raises(PimError) as exc:
        reg.add_join("239.2.2.2", None, "eth0")
    assert exc.value.code == "table-full"
    assert reg.entry_count() == 1
    assert reg.group_view("239.2.2.2")["star"] is None


def test_source_switch_updates_group_view():
    reg = PimJoinRegistry()
    reg.add_join("239.1.1.1", None, "eth0")
    reg.add_join("239.1.1.1", "10.0.0.1", "eth0")
    reg.add_join("239.1.1.1", "10.0.0.2", "eth1")
    reg.prune_join("239.1.1.1", "10.0.0.1", "eth0")
    view = reg.group_view("239.1.1.1")
    assert view["star"]["interfaces"] == ["eth0"]
    assert set(view["sources"]) == {"10.0.0.2"}
    assert view["sources"]["10.0.0.2"]["interfaces"] == ["eth1"]


def test_register_requires_directly_connected_source():
    reg = PimJoinRegistry()
    with pytest.raises(PimError) as exc:
        reg.register_source("239.1.1.1", "10.0.0.9", directly_connected=False)
    assert exc.value.code == "not-directly-connected"
    assert reg.register_source("239.1.1.1", "10.0.0.9", directly_connected=True) is True
    assert reg.is_registering("239.1.1.1", "10.0.0.9") is True


def test_register_stop_halts_encapsulation():
    reg = PimJoinRegistry()
    reg.register_source("239.1.1.1", "10.0.0.9")
    assert reg.register_stop("239.1.1.1", "10.0.0.9") is True
    assert reg.is_registering("239.1.1.1", "10.0.0.9") is False
    assert reg.register_stop("239.1.1.1", "10.0.0.9") is True


def test_assert_lost_prunes_interface():
    reg = PimJoinRegistry()
    reg.add_join("239.1.1.1", "10.0.0.1", "eth0")
    reg.add_join("239.1.1.1", "10.0.0.1", "eth1")
    assert reg.assert_lost("239.1.1.1", "10.0.0.1", "eth0") is True
    assert reg.outgoing("239.1.1.1", "10.0.0.1") == ["eth1"]


def test_ssm_requires_source():
    reg = PimJoinRegistry()
    with pytest.raises(PimError) as exc:
        reg.add_join("232.1.1.1", None, "eth0", mode="ssm")
    assert exc.value.code == "source-required"
    reg.add_join("232.1.1.1", "10.0.0.5", "eth0", mode="ssm")
    assert reg.mode_of("232.1.1.1", "10.0.0.5") == "ssm"
    assert reg.outgoing("232.1.1.1", "10.0.0.5") == ["eth0"]


def test_rp_tiebreak_uses_hash():
    reg = PimJoinRegistry()
    low_hash = {"name": "rp-a", "priority": 10, "hash": 1}
    high_hash = {"name": "rp-b", "priority": 10, "hash": 9}
    assert reg.elect_rp([low_hash, high_hash]) == "rp-b"
    assert reg.elect_rp([high_hash, low_hash]) == "rp-b"
    assert reg.elect_rp([
        {"name": "rp-a", "priority": 5, "hash": 1},
        high_hash,
    ]) == "rp-a"


def test_snapshot_restore_rejoins():
    reg = PimJoinRegistry()
    reg.add_join("239.1.1.1", None, "eth0")
    reg.add_join("239.1.1.1", "10.0.0.1", "eth0")
    snapshot = reg.snapshot()
    restored = PimJoinRegistry()
    restored.restore(snapshot)
    assert restored.entry_count() == 2
    rejoin = restored.rejoin_list()
    assert len(rejoin) == 2
    assert {item["group"] for item in rejoin} == {"239.1.1.1"}


def test_prune_removes_only_named_interface():
    reg = PimJoinRegistry()
    reg.add_join("239.1.1.1", None, "eth0")
    reg.add_join("239.1.1.1", None, "eth1")
    assert reg.prune_join("239.1.1.1", None, "eth0") is True
    assert reg.outgoing("239.1.1.1") == ["eth1"]
    assert reg.prune_join("239.1.1.1", None, "eth0") is False
