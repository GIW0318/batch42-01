import pytest

import mailbox_attachment_expiry
from mailbox_attachment_expiry import (
    InventoryFullError,
    MailboxAttachmentExpiry,
    MailboxError,
    MailExpiredError,
)


def test_claim_is_idempotent_for_same_mail():
    box = MailboxAttachmentExpiry(inventory_capacity=10)
    box.deliver("m1", "sender", ["sword", "shield"], expires_at=1000)
    first = box.claim("m1", "player")
    assert first["status"] == "claimed"
    assert first["items"] == ["sword", "shield"]
    second = box.claim("m1", "player")
    assert second["status"] == "already_received"
    assert box.inventory_items("player") == ["sword", "shield"]


def test_concurrent_begin_claim_only_one_pending():
    box = MailboxAttachmentExpiry(inventory_capacity=10)
    box.deliver("m1", "sender", ["gem"], expires_at=1000)
    first = box.begin_claim("m1", "player-a")
    assert first["status"] == "pending"
    second = box.begin_claim("m1", "player-b")
    assert second["status"] == "already_received"
    committed = box.commit_claim(first["tx_id"])
    assert committed["status"] == "claimed"
    assert box.is_claimed("m1") is True
    assert box.inventory_items("player-a") == ["gem"]
    assert box.inventory_items("player-b") == []


def test_inventory_full_rejects_whole_mail_without_partial_claim():
    box = MailboxAttachmentExpiry(inventory_capacity=1)
    box.deliver("m1", "sender", ["a", "b", "c"], expires_at=1000)
    with pytest.raises(InventoryFullError) as exc:
        box.claim("m1", "player")
    assert exc.value.code == "inventory_full"
    assert box.inventory_items("player") == []
    assert box.is_claimed("m1") is False


def test_claim_uses_server_time_after_expiry():
    box = MailboxAttachmentExpiry(server_time=100)
    box.deliver("m1", "sender", ["x"], expires_at=200)
    box.advance(150)
    with pytest.raises(MailExpiredError) as exc:
        box.claim("m1", "player")
    assert exc.value.code == "mail_expired"
    box.deliver("m2", "sender", ["y"], expires_at=500)
    assert box.claim("m2", "player")["status"] == "claimed"


def test_return_failure_restores_mail_for_claim():
    box = MailboxAttachmentExpiry(inventory_capacity=5)
    box.deliver("m1", "ghost", ["x", "y"], expires_at=1000)
    box.delete_account("ghost")
    result = box.return_to_sender("m1")
    assert result["status"] == "return_failed"
    assert result["reason"] == "sender_missing"
    assert box.is_claimed("m1") is False
    assert box.claim("m1", "player")["items"] == ["x", "y"]


def test_return_and_claim_are_mutually_exclusive():
    box = MailboxAttachmentExpiry(inventory_capacity=5)
    box.deliver("m1", "sender", ["x"], expires_at=1000)
    assert box.return_to_sender("m1")["status"] == "returned"
    with pytest.raises(MailboxError) as exc:
        box.claim("m1", "player")
    assert exc.value.code == "mail_returned"
    assert box.inventory_items("player") == []

    box.deliver("m2", "sender", ["y"], expires_at=1000)
    box.claim("m2", "player")
    with pytest.raises(MailboxError) as exc:
        box.return_to_sender("m2")
    assert exc.value.code == "mail_claimed"


def test_restart_restores_claimed_mail_and_read_flag():
    box = MailboxAttachmentExpiry(inventory_capacity=5)
    box.deliver("m1", "sender", ["x"], expires_at=1000)
    box.open_mail("m1", "player")
    box.claim("m1", "player")
    snapshot = box.snapshot()

    restored = MailboxAttachmentExpiry()
    restored.restore(snapshot)
    assert restored.is_claimed("m1") is True
    assert restored.mail_state("m1")["read"] is True
    assert restored.inventory_items("player") == ["x"]
    assert restored.claim("m1", "player")["status"] == "already_received"


def test_restart_rolls_back_pending_claim():
    box = MailboxAttachmentExpiry(inventory_capacity=5)
    box.deliver("m1", "sender", ["x"], expires_at=1000)
    tx = box.begin_claim("m1", "player")
    assert tx["status"] == "pending"
    snapshot = box.snapshot()

    restored = MailboxAttachmentExpiry()
    restored.restore(snapshot)
    restored.recover()
    assert restored.is_claimed("m1") is False
    assert restored.inventory_items("player") == []
    assert restored.claim("m1", "player")["status"] == "claimed"


def test_duplicate_mail_id_is_rejected_and_single_visible():
    box = MailboxAttachmentExpiry(inventory_capacity=5)
    box.deliver("m1", "sender", ["x"], expires_at=1000)
    with pytest.raises(MailboxError) as exc:
        box.deliver("m1", "sender-2", ["y"], expires_at=1000)
    assert exc.value.code == "duplicate_mail"
    assert len(box.visible_mails()) == 1
    assert box.claim("m1", "player")["items"] == ["x"]


def test_cross_server_missing_attachment_rejects_claim():
    box = MailboxAttachmentExpiry(inventory_capacity=5,
                                  item_catalog={"local_sword": 1})
    box.deliver("m1", "sender", ["local_sword", "missing_ring"],
                expires_at=1000, cross_server=True)
    with pytest.raises(MailboxError) as exc:
        box.claim("m1", "player")
    assert exc.value.code == "item_unavailable"
    assert box.is_claimed("m1") is False
    assert box.inventory_items("player") == []


def test_expired_limited_attachment_blocks_then_can_be_removed():
    box = MailboxAttachmentExpiry(inventory_capacity=5)
    box.deliver("m1", "sender", ["event_blade", "potion"], expires_at=1000)
    box.expire_attachment("m1", "event_blade")
    with pytest.raises(MailboxError) as exc:
        box.claim("m1", "player")
    assert exc.value.code == "attachment_expired"
    assert box.is_claimed("m1") is False
    assert box.inventory_items("player") == []
    assert box.remove_expired_attachments("m1") == ["event_blade"]
    assert box.claim("m1", "player")["items"] == ["potion"]


def test_sweep_keeps_one_recoverable_copy():
    box = MailboxAttachmentExpiry(server_time=0, sweep_retention_seconds=100)
    box.deliver("m1", "sender", ["x"], expires_at=50)
    box.advance(60)
    swept = box.sweep_expired()
    assert [entry["mail_id"] for entry in swept] == ["m1"]
    copy = box.swept_copy("m1")
    assert copy["items"] == ["x"]
    assert copy["recoverable_until"] == 160
    assert box.sweep_expired() == []


def test_batch_claim_is_atomic_when_inventory_short():
    box = MailboxAttachmentExpiry(inventory_capacity=5)
    box.deliver("m1", "sender", ["a", "b"], expires_at=1000)
    box.deliver("m2", "sender", ["c", "d", "e"], expires_at=1000)
    with pytest.raises(InventoryFullError) as exc:
        box.batch_claim(["m1", "m2"], "player")
    assert exc.value.code == "inventory_full"
    assert box.inventory_items("player") == []
    assert box.mail_state("m1")["read"] is False
    assert box.mail_state("m2")["read"] is False


def test_batch_claim_marks_all_read_on_success():
    box = MailboxAttachmentExpiry(inventory_capacity=6)
    box.deliver("m1", "sender", ["a", "b"], expires_at=1000)
    box.deliver("m2", "sender", ["c", "d"], expires_at=1000)
    result = box.batch_claim(["m1", "m2"], "player")
    assert result["claimed"] == ["m1", "m2"]
    assert box.mail_state("m1")["read"] is True
    assert box.mail_state("m2")["read"] is True
    assert box.inventory_items("player") == ["a", "b", "c", "d"]


def test_sweep_skips_claimed_and_deleted_mail_keeps_copy():
    box = MailboxAttachmentExpiry(inventory_capacity=5, server_time=0,
                                  sweep_retention_seconds=100)
    box.deliver("m1", "sender", ["x"], expires_at=50)
    box.claim("m1", "player")
    box.deliver("m2", "sender", ["y"], expires_at=50)
    deleted = box.delete_mail("m2")
    assert deleted["recovered_items"] == ["y"]
    box.advance(60)
    assert box.sweep_expired() == []
    assert box.mail_state("m1")["status"] == "claimed"
    with pytest.raises(MailboxError) as exc:
        box.claim("m2", "other")
    assert exc.value.code == "mail_not_found"
