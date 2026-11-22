"""Crafting queue station.

Baseline shell: every method raises NotImplementedError.

Contract notes for the implementation:

* A recipe snapshot is ``{"version", "materials", "seconds", "output",
  "durability_cost"}``.  ``enqueue`` deducts ``materials * quantity``,
  consumes station durability and appends the order.  A broken station
  (durability <= 0) raises ``StationBrokenError``; a full queue raises
  ``QueueFullError``; neither may deduct materials.
* Only the unconsumed part of an order is refunded on cancel:
  ``refund[material] = amount * remaining_seconds // seconds``.  Waiting
  orders refund in full, an order at half progress refunds half.  Each
  cancel appends one refund log entry keyed by order id.
* ``advance`` distributes elapsed seconds across each character's orders in
  queue order.  Remaining time shrinks and a completed order becomes
  ``ready``; ready orders keep their output until claimed.
* ``claim`` is idempotent per order: one success, every later call returns
  ``already_claimed`` and adds nothing.  An order that is crafting or ready
  cannot be reordered; reordering never charges materials again.
* ``reset_profession`` fails and refunds the character's orders.  An
  expired recipe fails its queued orders and refunds the original snapshot
  in full (never the discounted event price).
* ``export_state`` / ``load_state`` preserve remaining seconds, invested
  materials and recipe snapshots without re-charging the player.
"""


class StationBrokenError(Exception):
    """Raised when the station durability is zero."""


class QueueFullError(Exception):
    """Raised when the character queue is full."""


class InsufficientMaterialsError(Exception):
    """Raised when the character lacks the recipe materials."""


class CraftingQueueStation:
    def __init__(self, recipes=None, inventory=None, durability=100, capacity=5):
        raise NotImplementedError

    def add_recipe(
        self, recipe_id, version, materials, seconds, output, durability_cost=10
    ):
        raise NotImplementedError

    def expire_recipe(self, recipe_id):
        raise NotImplementedError

    def enqueue(self, order_id, character, recipe_id, quantity=1, now=0.0):
        raise NotImplementedError

    def cancel(self, order_id, now=0.0):
        raise NotImplementedError

    def claim(self, order_id):
        raise NotImplementedError

    def reorder(self, order_id, position):
        raise NotImplementedError

    def advance(self, seconds, now=0.0):
        raise NotImplementedError

    def progress(self, order_id):
        raise NotImplementedError

    def refund_log(self, order_id):
        raise NotImplementedError

    def inventory(self, character):
        raise NotImplementedError

    def queue(self, character):
        raise NotImplementedError

    def reset_profession(self, character):
        raise NotImplementedError

    def set_durability(self, value):
        raise NotImplementedError

    def export_state(self):
        raise NotImplementedError

    def load_state(self, state):
        raise NotImplementedError
