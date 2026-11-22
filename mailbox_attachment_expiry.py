"""Mail attachment claim, expiry, return and sweep ledger (skeleton).

Claim is idempotent per mail id; expiry and claim are mutually exclusive;
inventory capacity is all-or-nothing per mail.  The server clock is the only
time source.  Nothing here implements the behaviour yet.
"""

MAIL_NOT_FOUND = "mail_not_found"
MAIL_EXPIRED = "mail_expired"
ALREADY_RECEIVED = "already_received"
INVENTORY_FULL = "inventory_full"
DUPLICATE_MAIL = "duplicate_mail"
SENDER_MISSING = "sender_missing"
MAIL_RETURNED = "mail_returned"
MAIL_CLAIMED = "mail_claimed"
ITEM_UNAVAILABLE = "item_unavailable"
ATTACHMENT_EXPIRED = "attachment_expired"


class MailboxError(Exception):
    def __init__(self, code, message=""):
        self.code = code
        super().__init__(code if not message else "%s: %s" % (code, message))


class InventoryFullError(MailboxError):
    pass


class MailExpiredError(MailboxError):
    pass


class MailboxAttachmentExpiry:
    def __init__(self, inventory_capacity=20, server_time=0,
                 sweep_retention_seconds=3600, item_catalog=None):
        self.inventory_capacity = inventory_capacity
        self.server_time = server_time
        self.sweep_retention_seconds = sweep_retention_seconds
        self.item_catalog = dict(item_catalog or {})

    def advance(self, seconds):
        raise NotImplementedError

    def now(self):
        raise NotImplementedError

    def deliver(self, mail_id, sender_id, items, expires_at=None,
                cross_server=False):
        raise NotImplementedError

    def visible_mails(self):
        raise NotImplementedError

    def mail_state(self, mail_id):
        raise NotImplementedError

    def is_claimed(self, mail_id):
        raise NotImplementedError

    def open_mail(self, mail_id, player_id):
        raise NotImplementedError

    def claim(self, mail_id, player_id):
        raise NotImplementedError

    def begin_claim(self, mail_id, player_id):
        raise NotImplementedError

    def commit_claim(self, tx_id):
        raise NotImplementedError

    def abort_claim(self, tx_id):
        raise NotImplementedError

    def batch_claim(self, mail_ids, player_id):
        raise NotImplementedError

    def return_to_sender(self, mail_id):
        raise NotImplementedError

    def delete_mail(self, mail_id):
        raise NotImplementedError

    def delete_account(self, account_id):
        raise NotImplementedError

    def expire_attachment(self, mail_id, item_id):
        raise NotImplementedError

    def remove_expired_attachments(self, mail_id):
        raise NotImplementedError

    def sweep_expired(self):
        raise NotImplementedError

    def swept_copy(self, mail_id):
        raise NotImplementedError

    def inventory_items(self, player_id):
        raise NotImplementedError

    def transaction_log(self):
        raise NotImplementedError

    def snapshot(self):
        raise NotImplementedError

    def restore(self, snapshot):
        raise NotImplementedError

    def recover(self):
        raise NotImplementedError
