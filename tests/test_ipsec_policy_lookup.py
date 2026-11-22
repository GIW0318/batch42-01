from concurrent.futures import ThreadPoolExecutor

import pytest

from ipsec_policy_lookup import IpsecError, IpsecPolicyTable
import ipsec_policy_lookup


def test_overlapping_selectors_with_same_priority_conflict():
    table = IpsecPolicyTable()
    table.add_policy("10.0.0.0/8", priority=100)
    with pytest.raises(IpsecError) as exc:
        table.add_policy("10.1.0.0/16", priority=100)
    assert exc.value.code == "EEXIST"


def test_longest_prefix_is_deterministic():
    table = IpsecPolicyTable()
    broad = table.add_policy("10.1.0.0/16", priority=200)
    narrow = table.add_policy("10.1.2.0/24", priority=100)
    assert table.lookup("10.1.2.5", "10.9.0.1") == narrow
    assert table.lookup("10.1.3.5", "10.9.0.1") == broad


def test_delete_policy_atomically_removes_associated_sa():
    table = IpsecPolicyTable()
    policy_id = table.add_policy("10.0.0.0/8", priority=100)
    table.install_sa(policy_id, spi=0x1001)
    assert table.sa_count() == 1
    table.delete_policy(policy_id)
    assert table.sa_count() == 0
    with pytest.raises(IpsecError) as exc:
        table.select_sa(policy_id)
    assert exc.value.code == "NO_SA"


def test_restart_restores_policy_sa_pairing():
    table = IpsecPolicyTable()
    policy_id = table.add_policy("192.168.0.0/16", priority=50)
    table.install_sa(policy_id, spi=0x2002, enc="aes256gcm")
    snapshot = table.snapshot()
    restored = IpsecPolicyTable()
    restored.restore(snapshot)
    assert restored.health()["ok"] is True
    assert restored.select_sa(policy_id)["spi"] == 0x2002


def test_nat_t_sa_is_found_by_translated_port():
    table = IpsecPolicyTable()
    policy_id = table.add_policy("172.16.0.0/12", priority=100)
    table.install_sa(policy_id, spi=0x3003, port=4500)
    assert table.sa_for_port(policy_id, 4500)["spi"] == 0x3003
    assert table.sa_for_port(policy_id, 500) is None


def test_dual_subnet_traffic_does_not_cross_tunnels():
    table = IpsecPolicyTable()
    broad = table.add_policy("10.1.0.0/16", priority=200)
    narrow = table.add_policy("10.1.2.0/24", priority=100)
    assert table.lookup("10.1.2.9", "10.9.0.1") == narrow
    assert table.lookup("10.1.3.9", "10.9.0.1") == broad


def test_deleting_policies_does_not_leak_sa_entries():
    table = IpsecPolicyTable()
    policy_ids = []
    for index in range(10):
        policy_id = table.add_policy("10.%d.0.0/16" % index, priority=100 + index)
        table.install_sa(policy_id, spi=0x4000 + index)
        policy_ids.append(policy_id)
    for policy_id in policy_ids[:5]:
        table.delete_policy(policy_id)
    assert table.policy_count() == 5
    assert table.sa_count() == 5


def test_one_way_policy_is_detected():
    table = IpsecPolicyTable()
    policy_id = table.add_policy("10.0.0.0/8", priority=100, direction="out")
    with pytest.raises(IpsecError) as exc:
        table.validate_bidirectional(policy_id)
    assert exc.value.code == "ONE_WAY_POLICY"


def test_bidirectional_policy_pair_validates():
    table = IpsecPolicyTable()
    policy_id = table.add_policy_pair("10.0.0.0/8", priority=100)
    assert table.validate_bidirectional(policy_id) is True
    assert table.lookup("10.2.3.4", "10.9.0.1", direction="in") is not None


def test_replay_window_is_atomic_under_concurrency():
    table = IpsecPolicyTable()
    policy_id = table.add_policy("10.0.0.0/8", priority=100)
    table.install_sa(policy_id, spi=0x5005)

    def attempt(seq):
        return table.check_replay(0x5005, seq)

    with ThreadPoolExecutor(max_workers=16) as pool:
        accepted = list(pool.map(attempt, range(1, 201)))
    assert all(accepted)
    with ThreadPoolExecutor(max_workers=16) as pool:
        replayed = list(pool.map(attempt, range(1, 201)))
    assert not any(replayed)


def test_weak_dh_group_is_rejected():
    table = IpsecPolicyTable()
    policy_id = table.add_policy("10.0.0.0/8", priority=100)
    with pytest.raises(IpsecError) as exc:
        table.install_sa(policy_id, spi=0x6006, dh_group=1)
    assert exc.value.code == "NO_PROPOSAL_CHOSEN"
    assert table.install_sa(policy_id, spi=0x6007, dh_group=14)["dh_group"] == 14


def test_policy_update_invalidates_old_sa():
    table = IpsecPolicyTable()
    policy_id = table.add_policy("10.0.0.0/8", priority=100, proposal="aes128")
    table.install_sa(policy_id, spi=0x7007, enc="aes128")
    table.update_proposal(policy_id, "aes256gcm")
    assert table.sa_count() == 0
    with pytest.raises(IpsecError) as exc:
        table.select_sa(policy_id)
    assert exc.value.code == "NO_SA"


def test_rekey_installs_new_sa_without_gap():
    table = IpsecPolicyTable()
    policy_id = table.add_policy("10.0.0.0/8", priority=100)
    table.install_sa(policy_id, spi=0x8008)
    result = table.rekey(policy_id, new_spi=0x8009)
    assert result["spi"] == 0x8009
    assert table.select_sa(policy_id)["spi"] == 0x8009
    assert table.list_sa_spis(policy_id) == [0x8009]


def test_restart_detects_policy_without_sa():
    table = IpsecPolicyTable()
    policy_id = table.add_policy("10.0.0.0/8", priority=100)
    snapshot = table.snapshot()
    restored = IpsecPolicyTable()
    restored.restore(snapshot)
    health = restored.health()
    assert health["ok"] is False
    assert health["missing_sa"] == [policy_id]


def test_lookup_without_matching_policy_is_denied():
    table = IpsecPolicyTable()
    table.add_policy("10.0.0.0/8", priority=100)
    with pytest.raises(IpsecError) as exc:
        table.lookup("192.0.2.1", "10.9.0.1")
    assert exc.value.code == "NO_POLICY"
