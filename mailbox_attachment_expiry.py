"""Mail attachment claim, expiry, return and sweep ledger.

Claim is idempotent per mail id; expiry and claim are mutually exclusive;
inventory capacity is all-or-nothing per mail.  The server clock is the only
time source.  Swept mails keep a short-lived recoverable copy, and unfinished
claims are rolled back from the transaction log after a restart.
"""

import copy

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
        self._mails = {}
        self._inventories = {}
        self._deleted_accounts = set()
        self._swept = {}
        self._transactions = {}
        self._tx_log = []
        self._tx_counter = 0

    def advance(self, seconds):
        self.server_time += seconds

    def now(self):
        return self.server_time

    def _get_mail(self, mail_id):
        mail = self._mails.get(mail_id)
        if mail is None:
            raise MailboxError(MAIL_NOT_FOUND, mail_id)
        return mail

    def _is_expired(self, mail):
        return (mail["expires_at"] is not None
                and self.server_time >= mail["expires_at"])

    def _ensure_capacity(self, player_id, count):
        used = len(self._inventories.get(player_id, []))
        if used + count >= self.inventory_capacity:
            raise InventoryFullError(INVENTORY_FULL, player_id)

    def _validate_claimable(self, mail):
        if mail["status"] == "returned":
            raise MailboxError(MAIL_RETURNED, mail["mail_id"])
        if self._is_expired(mail):
            raise MailExpiredError(MAIL_EXPIRED, mail["mail_id"])
        if mail["cross_server"]:
            missing = [item for item in mail["items"]
                       if item not in self.item_catalog]
            if missing:
                raise MailboxError(ITEM_UNAVAILABLE,
                                   ",".join(sorted(missing)))
        if mail["expired_items"]:
            raise MailboxError(ATTACHMENT_EXPIRED,
                               ",".join(sorted(mail["expired_items"])))

    def _log_tx(self, tx_id, mail_id, player_id, action):
        self._tx_log.append({
            "tx_id": tx_id,
            "mail_id": mail_id,
            "player_id": player_id,
            "action": action,
            "at": self.server_time,
        })

    def deliver(self, mail_id, sender_id, items, expires_at=None,
                cross_server=False):
        if mail_id in self._mails or mail_id in self._swept:
            raise MailboxError(DUPLICATE_MAIL, mail_id)
        self._mails[mail_id] = {
            "mail_id": mail_id,
            "sender_id": sender_id,
            "items": list(items),
            "expires_at": expires_at,
            "cross_server": cross_server,
            "status": "active",
            "read": False,
            "expired_items": set(),
            "pending_tx": None,
            "claimed_by": None,
        }
        return self.mail_state(mail_id)

    def visible_mails(self):
        return [self.mail_state(mail_id) for mail_id, mail in self._mails.items()
                if mail["status"] in ("active", "claimed")]

    def mail_state(self, mail_id):
        mail = self._get_mail(mail_id)
        return {
            "mail_id": mail["mail_id"],
            "sender_id": mail["sender_id"],
            "items": list(mail["items"]),
            "expires_at": mail["expires_at"],
            "cross_server": mail["cross_server"],
            "status": mail["status"],
            "read": mail["read"],
        }

    def is_claimed(self, mail_id):
        mail = self._mails.get(mail_id)
        return mail is not None and mail["status"] == "claimed"

    def open_mail(self, mail_id, player_id):
        mail = self._get_mail(mail_id)
        mail["read"] = True
        return self.mail_state(mail_id)

    def claim(self, mail_id, player_id):
        begun = self.begin_claim(mail_id, player_id)
        if begun["status"] != "pending":
            return begun
        return self.commit_claim(begun["tx_id"])

    def begin_claim(self, mail_id, player_id):
        mail = self._get_mail(mail_id)
        if mail["status"] == "claimed" or mail["pending_tx"] is not None:
            return {"status": ALREADY_RECEIVED, "mail_id": mail_id}
        self._validate_claimable(mail)
        self._ensure_capacity(player_id, len(mail["items"]))
        self._tx_counter += 1
        tx_id = "tx-%d" % self._tx_counter
        self._transactions[tx_id] = {
            "tx_id": tx_id,
            "mail_id": mail_id,
            "player_id": player_id,
            "status": "pending",
        }
        mail["pending_tx"] = tx_id
        self._log_tx(tx_id, mail_id, player_id, "begin")
        return {"status": "pending", "tx_id": tx_id, "mail_id": mail_id}

    def commit_claim(self, tx_id):
        tx = self._transactions.get(tx_id)
        if tx is None or tx["status"] != "pending":
            raise MailboxError(MAIL_NOT_FOUND, tx_id)
        mail = self._get_mail(tx["mail_id"])
        self._ensure_capacity(tx["player_id"], len(mail["items"]))
        inventory = self._inventories.setdefault(tx["player_id"], [])
        inventory.extend(mail["items"])
        mail["status"] = "claimed"
        mail["read"] = True
        mail["pending_tx"] = None
        mail["claimed_by"] = tx["player_id"]
        tx["status"] = "committed"
        self._log_tx(tx_id, mail["mail_id"], tx["player_id"], "commit")
        return {"status": "claimed", "mail_id": mail["mail_id"],
                "items": list(mail["items"])}

    def abort_claim(self, tx_id):
        tx = self._transactions.get(tx_id)
        if tx is None or tx["status"] != "pending":
            raise MailboxError(MAIL_NOT_FOUND, tx_id)
        mail = self._mails.get(tx["mail_id"])
        if mail is not None and mail["pending_tx"] == tx_id:
            mail["pending_tx"] = None
        tx["status"] = "aborted"
        self._log_tx(tx_id, tx["mail_id"], tx["player_id"], "abort")
        return {"status": "aborted", "tx_id": tx_id}

    def batch_claim(self, mail_ids, player_id):
        claimable = []
        total_items = 0
        for mail_id in mail_ids:
            mail = self._get_mail(mail_id)
            if mail["status"] == "claimed" or mail["pending_tx"] is not None:
                continue
            self._validate_claimable(mail)
            claimable.append(mail)
            total_items += len(mail["items"])
        self._ensure_capacity(player_id, total_items)
        inventory = self._inventories.setdefault(player_id, [])
        claimed = []
        for mail in claimable:
            inventory.extend(mail["items"])
            mail["status"] = "claimed"
            mail["read"] = True
            mail["claimed_by"] = player_id
            claimed.append(mail["mail_id"])
        return {"status": "claimed", "claimed": claimed}

    def return_to_sender(self, mail_id):
        mail = self._get_mail(mail_id)
        if mail["status"] == "claimed" or mail["pending_tx"] is not None:
            raise MailboxError(MAIL_CLAIMED, mail_id)
        if mail["status"] == "returned":
            raise MailboxError(MAIL_RETURNED, mail_id)
        if mail["sender_id"] in self._deleted_accounts:
            return {"status": "return_failed", "reason": SENDER_MISSING,
                    "mail_id": mail_id}
        mail["status"] = "returned"
        return {"status": "returned", "mail_id": mail_id}

    def delete_mail(self, mail_id):
        mail = self._get_mail(mail_id)
        recovered = list(mail["items"])
        del self._mails[mail_id]
        return {"status": "deleted", "mail_id": mail_id,
                "recovered_items": recovered}

    def delete_account(self, account_id):
        self._deleted_accounts.add(account_id)

    def expire_attachment(self, mail_id, item_id):
        mail = self._get_mail(mail_id)
        if item_id in mail["items"]:
            mail["expired_items"].add(item_id)
        return item_id

    def remove_expired_attachments(self, mail_id):
        mail = self._get_mail(mail_id)
        removed = [item for item in mail["items"]
                   if item in mail["expired_items"]]
        mail["items"] = [item for item in mail["items"]
                         if item not in mail["expired_items"]]
        mail["expired_items"] = set()
        return removed

    def sweep_expired(self):
        swept = []
        for mail_id, mail in list(self._mails.items()):
            if mail["status"] != "active" or not self._is_expired(mail):
                continue
            entry = {
                "mail_id": mail_id,
                "sender_id": mail["sender_id"],
                "items": list(mail["items"]),
                "swept_at": self.server_time,
                "recoverable_until": (self.server_time
                                      + self.sweep_retention_seconds),
            }
            self._swept[mail_id] = entry
            del self._mails[mail_id]
            swept.append(entry)
        for mail_id, entry in list(self._swept.items()):
            if self.server_time >= entry["recoverable_until"]:
                del self._swept[mail_id]
        return swept

    def swept_copy(self, mail_id):
        entry = self._swept.get(mail_id)
        return copy.deepcopy(entry) if entry is not None else None

    def inventory_items(self, player_id):
        return list(self._inventories.get(player_id, []))

    def transaction_log(self):
        return list(self._tx_log)

    def snapshot(self):
        return {
            "inventory_capacity": self.inventory_capacity,
            "server_time": self.server_time,
            "sweep_retention_seconds": self.sweep_retention_seconds,
            "item_catalog": copy.deepcopy(self.item_catalog),
            "mails": copy.deepcopy(self._mails),
            "inventories": copy.deepcopy(self._inventories),
            "deleted_accounts": set(self._deleted_accounts),
            "swept": copy.deepcopy(self._swept),
            "transactions": copy.deepcopy(self._transactions),
            "tx_log": copy.deepcopy(self._tx_log),
            "tx_counter": self._tx_counter,
        }

    def restore(self, snapshot):
        self.inventory_capacity = snapshot["inventory_capacity"]
        self.server_time = snapshot["server_time"]
        self.sweep_retention_seconds = snapshot["sweep_retention_seconds"]
        self.item_catalog = copy.deepcopy(snapshot["item_catalog"])
        self._mails = copy.deepcopy(snapshot["mails"])
        self._inventories = copy.deepcopy(snapshot["inventories"])
        self._deleted_accounts = set(snapshot["deleted_accounts"])
        self._swept = copy.deepcopy(snapshot["swept"])
        self._transactions = copy.deepcopy(snapshot["transactions"])
        self._tx_log = copy.deepcopy(snapshot["tx_log"])
        self._tx_counter = snapshot["tx_counter"]

    def recover(self):
        for tx_id, tx in list(self._transactions.items()):
            if tx["status"] == "pending":
                self.abort_claim(tx_id)
