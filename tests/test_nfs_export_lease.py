import pytest

import nfs_export_lease
from nfs_export_lease import NfsLeaseError, NfsLeaseKeeper


def test_grace_rejects_new_open():
    keeper = NfsLeaseKeeper(exports=["/srv/export"])
    keeper.start_grace()
    token = keeper.setclientid("client-1", "owner-1")
    keeper.confirm("client-1", token)
    with pytest.raises(NfsLeaseError) as exc:
        keeper.open_file("client-1", "fh-1")
    assert exc.value.code == "NFS4ERR_GRACE"


def test_grace_accepts_reclaim():
    keeper = NfsLeaseKeeper(exports=["/srv/export"])
    keeper.start_grace()
    state_id = keeper.open_file("client-1", "fh-1", reclaim=True)
    assert isinstance(state_id, str) and state_id
    assert keeper.state_count("client-1") == 1


def test_duplicate_setclientid_rejected():
    keeper = NfsLeaseKeeper()
    keeper.setclientid("client-1", "owner-1")
    with pytest.raises(NfsLeaseError) as exc:
        keeper.setclientid("client-1", "owner-2")
    assert exc.value.code == "NFS4ERR_CLID_INUSE"


def test_expire_atomically_reclaims_lock_delegation_and_layout():
    keeper = NfsLeaseKeeper(lease_seconds=90)
    state_id = keeper.open_file("client-1", "fh-1")
    keeper.delegate("client-1", state_id)
    keeper.layoutget("client-1", state_id)
    keeper.acquire_lock("client-1", state_id, "lock-1")
    keeper.advance(91)
    expired = keeper.expire()
    assert state_id in expired
    assert keeper.state_count("client-1") == 0
    assert keeper.lock_count("client-1") == 0
    assert keeper.delegation_count("client-1") == 0
    with pytest.raises(NfsLeaseError) as exc:
        keeper.renew("client-1", state_id)
    assert exc.value.code == "NFS4ERR_BAD_STATEID"


def test_delegreturn_releases_file_lock():
    keeper = NfsLeaseKeeper()
    state_id = keeper.open_file("client-1", "fh-1")
    keeper.delegate("client-1", state_id)
    keeper.acquire_lock("client-1", state_id, "lock-1")
    keeper.delegreturn("client-1", state_id)
    other = keeper.open_file("client-2", "fh-1")
    assert keeper.acquire_lock("client-2", other, "lock-1") is True


def test_two_clients_get_distinct_state_ids_for_same_file():
    keeper = NfsLeaseKeeper()
    first = keeper.open_file("client-1", "fh-1")
    second = keeper.open_file("client-2", "fh-1")
    assert first != second
    assert keeper.state_count() == 2


def test_renew_after_long_queue_delay_is_still_valid():
    keeper = NfsLeaseKeeper(lease_seconds=90)
    state_id = keeper.open_file("client-1", "fh-1")
    keeper.advance(50)
    renewed = keeper.renew("client-1", state_id, queue_delay=50, now=50)
    assert renewed["state_id"] == state_id
    assert renewed["expires_at"] >= 140
    assert renewed["remaining"] > 0


def test_export_path_trailing_slash_is_normalized():
    keeper = NfsLeaseKeeper(exports=["/srv/export"])
    assert keeper.normalize_export("/srv/export/") == "/srv/export"
    assert keeper.authorize_export("/srv/export/") is True
    assert keeper.authorize_export("//srv//export") is True


def test_export_path_case_variant_is_not_authorized():
    keeper = NfsLeaseKeeper(exports=["/srv/export"])
    with pytest.raises(NfsLeaseError) as exc:
        keeper.authorize_export("/srv/EXPORT")
    assert exc.value.code == "NFS4ERR_ACCESS"
    with pytest.raises(NfsLeaseError) as exc:
        keeper.authorize_export("/srv/export/../etc")
    assert exc.value.code == "NFS4ERR_ACCESS"


def test_restart_restores_lease_and_pnfs_layout():
    keeper = NfsLeaseKeeper()
    state_id = keeper.open_file("client-1", "fh-1")
    layout = keeper.layoutget("client-1", state_id)
    snapshot = keeper.snapshot()
    restored = NfsLeaseKeeper()
    restored.restore(snapshot)
    assert restored.state_count("client-1") == 1
    assert restored.renew("client-1", state_id)["state_id"] == state_id
    assert restored.layoutget("client-1", state_id)["layout_id"] == layout["layout_id"]


def test_renew_after_expiry_returns_bad_stateid():
    keeper = NfsLeaseKeeper(lease_seconds=30)
    state_id = keeper.open_file("client-1", "fh-1")
    keeper.advance(31)
    keeper.expire()
    with pytest.raises(NfsLeaseError) as exc:
        keeper.renew("client-1", state_id)
    assert exc.value.code == "NFS4ERR_BAD_STATEID"


def test_callback_timeout_falls_back_to_no_delegation():
    keeper = NfsLeaseKeeper()
    state_id = keeper.open_file("client-1", "fh-1")
    keeper.delegate("client-1", state_id)
    mode = keeper.callback_timeout("client-1")
    assert mode == "none"
    assert keeper.delegation_count("client-1") == 0
    assert keeper.renew("client-1", state_id)["state_id"] == state_id


def test_close_export_notifies_clients_and_invalidates_handles():
    keeper = NfsLeaseKeeper(exports=["/srv/export"])
    keeper.open_file("client-2", "fh-2")
    keeper.open_file("client-1", "fh-1")
    notified = keeper.close_export("/srv/export/")
    assert sorted(notified) == ["client-1", "client-2"]
    with pytest.raises(NfsLeaseError) as exc:
        keeper.open_file("client-1", "fh-1")
    assert exc.value.code == "NFS4ERR_STALE"


def test_disconnect_client_releases_all_state():
    keeper = NfsLeaseKeeper()
    state_id = keeper.open_file("client-1", "fh-1")
    keeper.acquire_lock("client-1", state_id, "lock-1")
    keeper.delegate("client-1", state_id)
    keeper.disconnect_client("client-1")
    assert keeper.state_count("client-1") == 0
    assert keeper.lock_count("client-1") == 0
    assert keeper.delegation_count("client-1") == 0


def test_state_limit_rejects_then_frees_after_disconnect():
    keeper = NfsLeaseKeeper(max_states=2)
    keeper.open_file("client-1", "fh-1")
    keeper.open_file("client-2", "fh-2")
    with pytest.raises(NfsLeaseError) as exc:
        keeper.open_file("client-3", "fh-3")
    assert exc.value.code == "NFS4ERR_RESOURCE"
    keeper.disconnect_client("client-1")
    assert keeper.state_count() == 1
    assert keeper.open_file("client-3", "fh-3")
