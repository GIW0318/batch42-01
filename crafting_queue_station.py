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

import copy
import threading


class StationBrokenError(Exception):
    """Raised when the station durability is zero."""


class QueueFullError(Exception):
    """Raised when the character queue is full."""


class InsufficientMaterialsError(Exception):
    """Raised when the character lacks the recipe materials."""


class RecipeExpiredError(ValueError):
    """Raised when a recipe can no longer be queued."""


class CraftingQueueStation:
    def __init__(self, recipes=None, inventory=None, durability=100, capacity=5):
        self._lock = threading.RLock()
        self._order_locks_guard = threading.Lock()
        self._order_locks = {}

        self._recipes = {}
        self._expired_recipes = set()
        self._expired_orders = set()

        self._inventory = {}
        for character, materials in (inventory or {}).items():
            self._inventory[character] = {
                material: int(amount) for material, amount in materials.items()
            }

        self._durability = durability
        self._capacity = int(capacity)
        self._orders = {}
        self._queues = {}
        self._refund_logs = {}

        for recipe_id, recipe in (recipes or {}).items():
            self.add_recipe(
                recipe_id,
                version=recipe["version"],
                materials=recipe["materials"],
                seconds=recipe["seconds"],
                output=recipe["output"],
                durability_cost=recipe.get("durability_cost", 10),
            )

    def add_recipe(
        self, recipe_id, version, materials, seconds, output, durability_cost=10
    ):
        if seconds <= 0:
            raise ValueError("recipe seconds must be positive")

        snapshot = {
            "version": version,
            "materials": copy.deepcopy(materials),
            "seconds": seconds,
            "output": copy.deepcopy(output),
            "durability_cost": durability_cost,
        }
        with self._lock:
            self._recipes[recipe_id] = snapshot
            self._expired_recipes.discard(recipe_id)
        return copy.deepcopy(snapshot)

    def expire_recipe(self, recipe_id):
        with self._lock:
            self._expired_recipes.add(recipe_id)
            for order_id, order in self._orders.items():
                if (
                    order["recipe_id"] == recipe_id
                    and order["status"] in ("waiting", "in_progress")
                ):
                    self._expired_orders.add(order_id)
        return {"status": "expired", "recipe_id": recipe_id}

    def enqueue(self, order_id, character, recipe_id, quantity=1, now=0.0):
        if quantity <= 0:
            raise ValueError("quantity must be positive")

        with self._lock:
            if order_id in self._orders:
                raise ValueError("order already exists: %s" % order_id)
            if self._durability <= 0:
                raise StationBrokenError("station durability is exhausted")

            queue_ids = self._active_queue_ids(character)
            if len(queue_ids) >= self._capacity:
                raise QueueFullError("character queue is full")
            position = len(queue_ids)

            if recipe_id in self._expired_recipes:
                raise RecipeExpiredError("recipe has expired: %s" % recipe_id)
            recipe = self._recipes.get(recipe_id)
            if recipe is None:
                raise ValueError("unknown recipe: %s" % recipe_id)

            invested = {
                material: amount * quantity
                for material, amount in recipe["materials"].items()
            }
            available = self._inventory.setdefault(character, {})
            missing = {
                material: amount - available.get(material, 0)
                for material, amount in invested.items()
                if available.get(material, 0) < amount
            }
            if missing:
                raise InsufficientMaterialsError(
                    "insufficient materials: %s" % sorted(missing)
                )

            for material, amount in invested.items():
                available[material] = available.get(material, 0) - amount

            durability_cost = recipe["durability_cost"] * quantity
            self._durability = max(0, self._durability - durability_cost)

            order = {
                "order_id": order_id,
                "character": character,
                "recipe_id": recipe_id,
                "recipe_version": recipe["version"],
                "quantity": quantity,
                "materials": copy.deepcopy(invested),
                "seconds": recipe["seconds"] * quantity,
                "remaining": recipe["seconds"] * quantity,
                "output": {
                    item: amount * quantity
                    for item, amount in recipe["output"].items()
                },
                "durability_cost": durability_cost,
                "status": "waiting",
                "created_at": now,
                "updated_at": now,
            }
            self._orders[order_id] = order
            self._queues.setdefault(character, []).append(order_id)

            return {
                "status": "queued",
                "order_id": order_id,
                "position": position,
                "remaining": order["remaining"],
                "recipe_version": order["recipe_version"],
            }

    def cancel(self, order_id, now=0.0):
        with self._order_lock(order_id):
            with self._lock:
                order = self._orders.get(order_id)
                if order is None:
                    return {"status": "not_found", "order_id": order_id}

                status = order["status"]
                if status == "claimed":
                    return {"status": "already_claimed", "order_id": order_id}
                if status in ("cancelled", "failed"):
                    return {"status": status, "order_id": order_id}
                if status == "ready":
                    return {
                        "status": "ready",
                        "order_id": order_id,
                        "refund": {},
                    }

                refund = self._refund_order(
                    order, reason="cancelled", now=now, full=False
                )
                order["status"] = "cancelled"
                order["updated_at"] = now
                self._remove_from_queue(order)
                return {
                    "status": "cancelled",
                    "order_id": order_id,
                    "refund": refund,
                }

    def claim(self, order_id):
        with self._order_lock(order_id):
            with self._lock:
                order = self._orders.get(order_id)
                if order is None:
                    return {"status": "not_found", "order_id": order_id}

                status = order["status"]
                if status == "claimed":
                    return {
                        "status": "already_claimed",
                        "order_id": order_id,
                        "items": {},
                    }
                if status in ("cancelled", "failed"):
                    return {"status": status, "order_id": order_id, "items": {}}
                if status != "ready":
                    return {
                        "status": "not_ready",
                        "order_id": order_id,
                        "items": {},
                    }

                items = copy.deepcopy(order["output"])
                character_inventory = self._inventory.setdefault(
                    order["character"], {}
                )
                for item, amount in items.items():
                    character_inventory[item] = (
                        character_inventory.get(item, 0) + amount
                    )

                order["status"] = "claimed"
                self._remove_from_queue(order)
                return {
                    "status": "claimed",
                    "order_id": order_id,
                    "items": items,
                }

    def reorder(self, order_id, position):
        with self._order_lock(order_id):
            with self._lock:
                order = self._orders.get(order_id)
                if order is None:
                    return {"status": "not_found", "order_id": order_id}

                status = order["status"]
                if status in ("in_progress", "ready", "claimed", "cancelled", "failed"):
                    return {
                        "status": status,
                        "order_id": order_id,
                        "position": self._position(order),
                    }

                queue_ids = self._active_queue_ids(order["character"])
                if order_id not in queue_ids:
                    queue_ids.append(order_id)
                queue_ids.remove(order_id)
                target = max(0, min(int(position), len(queue_ids)))
                queue_ids.insert(target, order_id)

                return {
                    "status": "reordered",
                    "order_id": order_id,
                    "position": target,
                }

    def advance(self, seconds, now=0.0):
        if seconds < 0:
            raise ValueError("seconds cannot be negative")

        completed = []
        failed = []
        with self._lock:
            for character, queue_ids in list(self._queues.items()):
                elapsed = seconds
                active_ids = [
                    order_id
                    for order_id in queue_ids
                    if self._orders.get(order_id, {}).get("status")
                    in ("waiting", "in_progress")
                ]

                for order_id in active_ids:
                    order = self._orders[order_id]
                    if self._order_has_expired_recipe(order):
                        self._refund_order(
                            order,
                            reason="recipe_expired",
                            now=now,
                            full=True,
                        )
                        order["status"] = "failed"
                        order["updated_at"] = now
                        self._expired_orders.discard(order_id)
                        self._remove_from_queue(order)
                        failed.append(order_id)

                if elapsed <= 0:
                    continue

                queue_ids = self._active_queue_ids(character)
                in_progress = [
                    order_id
                    for order_id in queue_ids
                    if self._orders[order_id]["status"] == "in_progress"
                ]
                waiting = [
                    order_id
                    for order_id in queue_ids
                    if self._orders[order_id]["status"] == "waiting"
                ]

                for order_id in in_progress + waiting:
                    if elapsed <= 0:
                        break
                    order = self._orders[order_id]
                    if order["status"] not in ("waiting", "in_progress"):
                        continue

                    consumed = min(elapsed, order["remaining"])
                    order["remaining"] -= consumed
                    elapsed -= consumed
                    order["updated_at"] = now

                    if order["remaining"] <= 0:
                        order["remaining"] = 0
                        order["status"] = "ready"
                        completed.append(order_id)
                    else:
                        order["status"] = "in_progress"

        return {"completed": completed, "failed": failed}

    def progress(self, order_id):
        with self._lock:
            order = self._orders.get(order_id)
            if order is None:
                return {"order_id": order_id, "status": "not_found"}
            return self._summary(order)

    def refund_log(self, order_id):
        with self._lock:
            return copy.deepcopy(self._refund_logs.get(order_id, []))

    def inventory(self, character):
        with self._lock:
            return copy.deepcopy(self._inventory.get(character, {}))

    def queue(self, character):
        with self._lock:
            return [
                self._summary(self._orders[order_id])
                for order_id in self._active_queue_ids(character)
                if order_id in self._orders
            ]

    def reset_profession(self, character):
        failed = []
        refunded = {}
        with self._lock:
            for order_id in list(self._active_queue_ids(character)):
                order = self._orders[order_id]
                if order["status"] in ("claimed", "cancelled", "failed"):
                    continue
                refund = self._refund_order(
                    order,
                    reason="profession_reset",
                    now=0.0,
                    full=(
                        order_id in self._expired_orders
                        or (
                            order["recipe_id"] in self._expired_recipes
                            and order["status"] in ("waiting", "in_progress")
                        )
                    ),
                )
                _merge_items(refunded, refund)
                order["status"] = "failed"
                self._expired_orders.discard(order_id)
                self._remove_from_queue(order)
                failed.append(order_id)
        return {"failed": failed, "refunded": refunded}

    def set_durability(self, value):
        with self._lock:
            self._durability = value
        return self._durability

    def export_state(self):
        with self._lock:
            return {
                "schema": 1,
                "durability": self._durability,
                "capacity": self._capacity,
                "inventory": copy.deepcopy(self._inventory),
                "recipes": copy.deepcopy(self._recipes),
                "expired_recipes": sorted(self._expired_recipes),
                "expired_orders": sorted(self._expired_orders),
                "orders": copy.deepcopy(self._orders),
                "queues": copy.deepcopy(self._queues),
                "refund_logs": copy.deepcopy(self._refund_logs),
            }

    def load_state(self, state):
        if state is None:
            raise ValueError("state is required")

        with self._lock:
            self._durability = state.get("durability", self._durability)
            self._capacity = int(state.get("capacity", self._capacity))
            if "inventory" in state:
                self._inventory = copy.deepcopy(state["inventory"])
            if "recipes" in state:
                self._recipes = copy.deepcopy(state["recipes"])
            self._expired_recipes = set(state.get("expired_recipes", []))
            self._expired_orders = set(state.get("expired_orders", []))
            self._orders = copy.deepcopy(state.get("orders", {}))
            self._refund_logs = copy.deepcopy(
                state.get("refund_logs", state.get("refund_log", {}))
            )

            queue_state = state.get("queues")
            if queue_state is None:
                self._queues = {}
                for order_id, order in self._orders.items():
                    if order.get("status") in (
                        "waiting",
                        "in_progress",
                        "ready",
                    ):
                        self._queues.setdefault(order["character"], []).append(
                            order_id
                        )
            else:
                self._queues = {}
                for character, order_ids in queue_state.items():
                    clean_ids = []
                    for order_id in order_ids:
                        order = self._orders.get(order_id)
                        if order is None:
                            continue
                        if order.get("status") in (
                            "waiting",
                            "in_progress",
                            "ready",
                        ):
                            clean_ids.append(order_id)
                    self._queues[character] = clean_ids

            with self._order_locks_guard:
                self._order_locks = {}
        return True

    def _order_lock(self, order_id):
        with self._order_locks_guard:
            lock = self._order_locks.get(order_id)
            if lock is None:
                lock = threading.RLock()
                self._order_locks[order_id] = lock
            return lock

    def _active_queue_ids(self, character):
        return self._queues.setdefault(character, [])

    def _position(self, order):
        try:
            return self._active_queue_ids(order["character"]).index(
                order["order_id"]
            )
        except ValueError:
            return None

    def _summary(self, order):
        summary = {
            "order_id": order["order_id"],
            "character": order["character"],
            "recipe_id": order["recipe_id"],
            "recipe_version": order["recipe_version"],
            "quantity": order["quantity"],
            "status": order["status"],
            "remaining": order["remaining"],
            "total_seconds": order["seconds"],
            "materials": copy.deepcopy(order["materials"]),
            "output": copy.deepcopy(order["output"]),
            "position": self._position(order),
        }
        return summary

    def _remove_from_queue(self, order):
        queue_ids = self._active_queue_ids(order["character"])
        try:
            queue_ids.remove(order["order_id"])
        except ValueError:
            pass

    def _order_has_expired_recipe(self, order):
        return (
            order["recipe_id"] in self._expired_recipes
            or order["order_id"] in self._expired_orders
        )

    def _refund_order(self, order, reason, now, full):
        if full:
            refund = copy.deepcopy(order["materials"])
        else:
            refund = {}
            total_seconds = order["seconds"]
            remaining = max(0, order["remaining"])
            for material, amount in order["materials"].items():
                if total_seconds <= 0:
                    refund_amount = amount
                else:
                    refund_amount = int(
                        amount * remaining // total_seconds
                    )
                refund[material] = refund_amount

        character_inventory = self._inventory.setdefault(order["character"], {})
        for material, amount in refund.items():
            character_inventory[material] = (
                character_inventory.get(material, 0) + amount
            )

        entry = {
            "order_id": order["order_id"],
            "character": order["character"],
            "recipe_id": order["recipe_id"],
            "recipe_version": order["recipe_version"],
            "reason": reason,
            "refund": copy.deepcopy(refund),
            "now": now,
        }
        self._refund_logs.setdefault(order["order_id"], []).append(entry)
        return refund


def _merge_items(target, items):
    for item, amount in items.items():
        target[item] = target.get(item, 0) + amount
