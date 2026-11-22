"""Face-to-face trade escrow.

Baseline shell: every method raises NotImplementedError.

Contract notes for the implementation:

* ``bags`` maps player -> ``{"gold": int, "items": {item_id:
  {"count": int, "level": int}}}``.  ``offer`` validates ownership without
  moving anything; once a side is locked its offer can never change again.
  Gold above ``gold_cap`` or below zero raises ``CurrencyOverflowError``;
  non-positive or over-owned item counts raise ``InvalidOfferError``.
* ``lock`` atomically moves the offered items and gold into that side's
  escrow.  A second lock attempt that cannot be covered returns
  ``insufficient`` and moves nothing.
* ``confirm`` settles only after both sides locked.  Exactly one settlement
  may happen; every later confirm returns ``trade_already_settled`` and
  moves nothing.  If the receiver lacks bag slots the whole trade fails,
  raises ``InventoryFullError`` and returns both escrows.
* ``cancel`` / ``disconnect`` return the escrow and clear it.  A settled
  trade can never be cancelled or settled again.
* ``apply_item_change`` simulates an enhance/salvage/transfer.  If the item
  belongs to an active trade it invalidates the trade, clears both locks
  and returns the escrow before applying the change.
* ``export_state`` / ``load_state`` preserve locked trades and escrow so a
  restart can continue or cancel without duplicating assets.
"""


class CurrencyOverflowError(Exception):
    """Raised when gold is negative or above the configured cap."""


class InvalidOfferError(Exception):
    """Raised for non-positive or over-owned item amounts."""


class InventoryFullError(Exception):
    """Raised when a settlement cannot fit the receiving bag."""


class TradeWindowEscrow:
    def __init__(self, bags=None, gold_cap=1000000, bag_slots=20, known_items=None):
        raise NotImplementedError

    def open(self, trade_id, a, b):
        raise NotImplementedError

    def offer(self, trade_id, side, items=None, gold=None):
        raise NotImplementedError

    def lock(self, trade_id, side):
        raise NotImplementedError

    def confirm(self, trade_id, side):
        raise NotImplementedError

    def cancel(self, trade_id, side=None):
        raise NotImplementedError

    def disconnect(self, trade_id, side):
        raise NotImplementedError

    def apply_item_change(self, player, item_id, level=None, count=None):
        raise NotImplementedError

    def inventory(self, player):
        raise NotImplementedError

    def escrow(self, trade_id):
        raise NotImplementedError

    def state(self, trade_id):
        raise NotImplementedError

    def reopen(self, trade_id):
        raise NotImplementedError

    def export_state(self):
        raise NotImplementedError

    def load_state(self, state):
        raise NotImplementedError
