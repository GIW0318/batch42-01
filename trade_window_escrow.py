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

import copy
import threading


class CurrencyOverflowError(Exception):
    """Raised when gold is negative or above the configured cap."""


class InvalidOfferError(Exception):
    """Raised for non-positive or over-owned item amounts."""


class InventoryFullError(Exception):
    """Raised when a settlement cannot fit the receiving bag."""


_SIDES = ("a", "b")
_TERMINAL_STATES = ("settled", "cancelled", "failed")


def _empty_holding():
    return {"items": {}, "gold": 0}


def _is_positive_int(value):
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


class TradeWindowEscrow:
    def __init__(self, bags=None, gold_cap=1000000, bag_slots=20, known_items=None):
        self._bags = bags if bags is not None else {}
        self._gold_cap = gold_cap
        self._bag_slots = bag_slots
        self._known_items = set(known_items) if known_items is not None else None
        self._trades = {}
        self._gate = threading.RLock()

    def _bag(self, player):
        return self._bags.setdefault(player, {"gold": 0, "items": {}})

    def _trade(self, trade_id):
        return self._trades[trade_id]

    def _bag_count(self, player, item_id):
        return self._bag(player)["items"].get(item_id, {}).get("count", 0)

    def _total_owned(self, player, item_id):
        """Everything the player still owns: bag plus all active escrow."""
        total = self._bag_count(player, item_id)
        for trade in self._trades.values():
            if trade["status"] in _TERMINAL_STATES:
                continue
            for side in _SIDES:
                if trade["players"][side] == player:
                    total += trade["escrow"][side]["items"].get(item_id, {}).get(
                        "count", 0
                    )
        return total

    @staticmethod
    def _add_item(bag, item_id, count, level):
        entry = bag["items"].get(item_id)
        if entry is None:
            bag["items"][item_id] = {"count": count, "level": level}
        else:
            entry["count"] += count

    def _refund(self, trade):
        for side in _SIDES:
            player = trade["players"][side]
            bag = self._bag(player)
            holding = trade["escrow"][side]
            bag["gold"] += holding["gold"]
            for item_id, entry in holding["items"].items():
                self._add_item(bag, item_id, entry["count"], entry["level"])
            trade["escrow"][side] = _empty_holding()
        trade["locked"] = {"a": False, "b": False}
        trade["confirmed"] = {"a": False, "b": False}

    def open(self, trade_id, a, b):
        with self._gate:
            if trade_id in self._trades:
                raise ValueError("trade already open: %s" % trade_id)
            self._trades[trade_id] = {
                "players": {"a": a, "b": b},
                "offers": {"a": _empty_holding(), "b": _empty_holding()},
                "escrow": {"a": _empty_holding(), "b": _empty_holding()},
                "locked": {"a": False, "b": False},
                "confirmed": {"a": False, "b": False},
                "status": "open",
            }
            return {"status": "open"}

    def offer(self, trade_id, side, items=None, gold=None):
        with self._gate:
            trade = self._trade(trade_id)
            if side not in _SIDES:
                raise InvalidOfferError("unknown side: %r" % (side,))
            if trade["status"] in _TERMINAL_STATES:
                return {"status": trade["status"]}
            if trade["locked"][side]:
                return {"status": "locked"}

            gold = 0 if gold is None else gold
            if not isinstance(gold, int) or isinstance(gold, bool):
                raise CurrencyOverflowError("gold must be an integer")
            if gold < 0 or gold > self._gold_cap:
                raise CurrencyOverflowError("gold out of range: %r" % (gold,))

            raw_items = {} if items is None else items
            player = trade["players"][side]
            clean_items = {}
            for item_id, count in raw_items.items():
                if self._known_items is not None and item_id not in self._known_items:
                    return {
                        "status": "unknown_item",
                        "reason": "unknown item: %s" % item_id,
                    }
                if not _is_positive_int(count):
                    raise InvalidOfferError(
                        "item count must be positive: %s=%r" % (item_id, count)
                    )
                if count > self._total_owned(player, item_id):
                    raise InvalidOfferError(
                        "not enough items: %s=%r" % (item_id, count)
                    )
                clean_items[item_id] = count

            trade["offers"][side] = {"items": clean_items, "gold": gold}
            return {"status": "offered"}

    def lock(self, trade_id, side):
        with self._gate:
            trade = self._trade(trade_id)
            if side not in _SIDES:
                raise InvalidOfferError("unknown side: %r" % (side,))
            if trade["status"] in _TERMINAL_STATES:
                return {"status": trade["status"]}
            if trade["locked"][side]:
                return {"status": "locked"}

            offer = trade["offers"][side]
            bag = self._bag(trade["players"][side])
            if bag["gold"] < offer["gold"]:
                return {"status": "insufficient", "reason": "not enough gold"}
            for item_id, count in offer["items"].items():
                owned = bag["items"].get(item_id, {}).get("count", 0)
                if owned < count:
                    return {"status": "insufficient", "reason": "not enough items"}

            bag["gold"] -= offer["gold"]
            escrow = trade["escrow"][side]
            escrow["gold"] += offer["gold"]
            for item_id, count in offer["items"].items():
                level = bag["items"][item_id]["level"]
                remaining = bag["items"][item_id]["count"] - count
                if remaining == 0:
                    del bag["items"][item_id]
                else:
                    bag["items"][item_id]["count"] = remaining
                escrow_entry = escrow["items"].setdefault(
                    item_id, {"count": 0, "level": level}
                )
                escrow_entry["count"] += count
            trade["locked"][side] = True
            return {"status": "locked"}

    def confirm(self, trade_id, side):
        with self._gate:
            trade = self._trade(trade_id)
            if side not in _SIDES:
                raise InvalidOfferError("unknown side: %r" % (side,))
            if trade["status"] == "settled":
                return {"status": "trade_already_settled", "settled": True}
            if trade["status"] == "cancelled":
                return {"status": "trade_cancelled"}
            if trade["status"] == "failed":
                return {"status": "trade_failed"}

            trade["confirmed"][side] = True
            if not all(trade["confirmed"][each] for each in _SIDES):
                return {"status": "confirmed"}
            return self._settle(trade)

    def _settle(self, trade):
        # Pre-check every receiver's bag capacity before moving anything,
        # so a short bag fails the whole trade atomically.
        for side in _SIDES:
            receiver_side = "b" if side == "a" else "a"
            receiver = self._bag(trade["players"][receiver_side])
            incoming = trade["escrow"][side]["items"]
            free_slots = self._bag_slots - len(receiver["items"])
            needed_slots = sum(
                1 for item_id in incoming if item_id not in receiver["items"]
            )
            if needed_slots > free_slots:
                self._refund(trade)
                trade["status"] = "failed"
                raise InventoryFullError(
                    "bag of %s cannot hold the incoming items"
                    % trade["players"][receiver_side]
                )

        for side in _SIDES:
            receiver_side = "b" if side == "a" else "a"
            receiver = self._bag(trade["players"][receiver_side])
            holding = trade["escrow"][side]
            receiver["gold"] += holding["gold"]
            for item_id, entry in holding["items"].items():
                self._add_item(
                    receiver, item_id, entry["count"], entry["level"]
                )
            trade["escrow"][side] = _empty_holding()
        trade["status"] = "settled"
        return {"status": "settled"}

    def cancel(self, trade_id, side=None):
        with self._gate:
            trade = self._trade(trade_id)
            if trade["status"] == "settled":
                return {"status": "trade_closed"}
            if trade["status"] in ("cancelled", "failed"):
                return {"status": trade["status"]}
            self._refund(trade)
            trade["status"] = "cancelled"
            return {"status": "cancelled"}

    def disconnect(self, trade_id, side):
        with self._gate:
            trade = self._trade(trade_id)
            if side not in _SIDES:
                raise InvalidOfferError("unknown side: %r" % (side,))
            if trade["status"] == "settled":
                return {"status": "trade_closed"}
            if trade["status"] in ("cancelled", "failed"):
                return {"status": trade["status"]}
            self._refund(trade)
            trade["status"] = "cancelled"
            return {"status": "cancelled", "reason": "peer disconnected"}

    def apply_item_change(self, player, item_id, level=None, count=None):
        with self._gate:
            invalidated = []
            for trade_id, trade in self._trades.items():
                if trade["status"] in _TERMINAL_STATES:
                    continue
                for side in _SIDES:
                    if (
                        trade["players"][side] == player
                        and item_id in trade["escrow"][side]["items"]
                    ):
                        self._refund(trade)
                        trade["status"] = "cancelled"
                        invalidated.append(trade_id)
                        break

            bag = self._bag(player)
            if item_id in bag["items"]:
                entry = bag["items"][item_id]
                if level is not None:
                    entry["level"] = level
                if count is not None:
                    if count <= 0:
                        del bag["items"][item_id]
                    else:
                        entry["count"] = count
            return {"invalidated": invalidated}

    def inventory(self, player):
        with self._gate:
            return copy.deepcopy(self._bag(player))

    def escrow(self, trade_id):
        with self._gate:
            trade = self._trade(trade_id)
            return copy.deepcopy(trade["escrow"])

    def state(self, trade_id):
        with self._gate:
            trade = self._trade(trade_id)
            if trade["status"] in _TERMINAL_STATES:
                return trade["status"]
            if any(trade["confirmed"][side] for side in _SIDES):
                return "pending_confirmation"
            if any(trade["locked"][side] for side in _SIDES):
                return "locked"
            return "open"

    def reopen(self, trade_id):
        with self._gate:
            trade = self._trade(trade_id)
            if trade["status"] == "settled":
                return {"status": "trade_closed"}
            self._refund(trade)
            trade["offers"] = {"a": _empty_holding(), "b": _empty_holding()}
            trade["status"] = "open"
            return {"status": "reopened"}

    def export_state(self):
        with self._gate:
            return copy.deepcopy({"bags": self._bags, "trades": self._trades})

    def load_state(self, state):
        with self._gate:
            self._bags = copy.deepcopy(state["bags"])
            self._trades = copy.deepcopy(state["trades"])
            return {"status": "loaded"}
