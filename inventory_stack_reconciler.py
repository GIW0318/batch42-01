from collections import defaultdict
from copy import deepcopy


class StackConflictError(Exception):
    def __init__(self, code="CONFLICT", message=""):
        super().__init__(message or code)
        self.code = code


class StackReconciler:
    _ALIASES = {
        "bag": "bag",
        "backpack": "bag",
        "inventory": "bag",
        "warehouse": "warehouse",
        "storage": "warehouse",
        "bank": "warehouse",
        "mail": "mail",
        "mailbox": "mail",
        "temp": "temp",
        "temporary": "temp",
        "drag": "temp",
        "guild": "guild",
        "guild_bank": "guild",
        "guild_warehouse": "guild",
    }

    def __init__(self, bag_size=200, warehouse_size=500, mail_size=50):
        self.bag_size = self._positive_int(bag_size, "bag_size")
        self.warehouse_size = self._positive_int(
            warehouse_size, "warehouse_size")
        self.mail_size = self._positive_int(mail_size, "mail_size")
        self._temp_size = self.bag_size
        self._guild_size = self.warehouse_size
        self._slots = {}
        self._journal = []
        self._applied = set()
        self._pending = []
        self._next_op = 1

    @staticmethod
    def _positive_int(value, name):
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise StackConflictError(
                "INVALID_ARGUMENT", "%s must be a positive integer" % name)
        return value

    @staticmethod
    def _count(value, name="count", allow_none=False):
        if value is None and allow_none:
            return None
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise StackConflictError(
                "INVALID_ARGUMENT", "%s must be a positive integer" % name)
        return value

    def _container(self, container):
        if not isinstance(container, str):
            raise StackConflictError(
                "INVALID_CONTAINER", "container name must be a string")
        canonical = self._ALIASES.get(container.lower())
        if canonical is None:
            raise StackConflictError(
                "INVALID_CONTAINER", "unknown container: %s" % container)
        return canonical

    def _capacity(self, container):
        if container == "bag":
            return self.bag_size
        if container == "warehouse":
            return self.warehouse_size
        if container == "mail":
            return self.mail_size
        if container == "temp":
            return self._temp_size
        if container == "guild":
            return self._guild_size
        raise StackConflictError(
            "INVALID_CONTAINER", "unknown container: %s" % container)

    def _key(self, ref):
        if isinstance(ref, dict):
            if "container" not in ref or "index" not in ref:
                raise StackConflictError(
                    "INVALID_SLOT", "slot reference needs container and index")
            container = ref["container"]
            index = ref["index"]
        elif isinstance(ref, (tuple, list)) and len(ref) == 2:
            container, index = ref
        elif isinstance(ref, str) and ":" in ref:
            container, index = ref.rsplit(":", 1)
        else:
            raise StackConflictError(
                "INVALID_SLOT", "slot reference must be (container, index)")
        container = self._container(container)
        if isinstance(index, bool) or not isinstance(index, int):
            raise StackConflictError(
                "INVALID_INDEX", "slot index must be an integer")
        if index < 0 or index >= self._capacity(container):
            raise StackConflictError(
                "INVALID_INDEX",
                "slot index %s is outside %s" % (index, container))
        return container, index

    @staticmethod
    def _clone_slot(slot):
        return dict(slot)

    @staticmethod
    def _bump(slot):
        slot["version"] = int(slot.get("version", 0)) + 1

    @staticmethod
    def _new_slot(item_id, count, bind, durability, max_stack):
        return {
            "item_id": item_id,
            "count": count,
            "bind": bool(bind),
            "durability": durability,
            "max_stack": max_stack,
            "version": 1,
        }

    def _compatible(self, slot, item_id, bind, durability, max_stack):
        return (
            slot["item_id"] == item_id
            and bool(slot["bind"]) == bool(bind)
            and slot["durability"] == durability
            and slot["max_stack"] == max_stack
        )

    def _validate_meta(self, item_id, count, bind, durability, max_stack):
        if item_id is None:
            raise StackConflictError(
                "INVALID_ITEM", "item_id must not be None")
        try:
            hash(item_id)
            hash(durability)
        except TypeError:
            raise StackConflictError(
                "INVALID_ITEM", "item identity must be hashable")
        self._count(count)
        self._positive_int(max_stack, "max_stack")
        return bool(bind)

    @staticmethod
    def _slot_view(container, index, slot):
        result = dict(slot)
        result["container"] = container
        result["index"] = index
        return result

    @staticmethod
    def _totals(slots, include_mail=True):
        totals = defaultdict(int)
        for (container, _), slot in slots.items():
            if container == "mail" and not include_mail:
                continue
            totals[slot["item_id"]] += slot["count"]
        return dict(totals)

    def _assert_conserved(self, action, item_id, count, before, after):
        before_totals = self._totals(before)
        after_totals = self._totals(after)
        if action in ("move", "split", "merge", "organize", "extract_mail"):
            if before_totals != after_totals:
                raise RuntimeError("inventory quantity conservation violated")
        elif action in ("destroy", "sell"):
            expected = dict(before_totals)
            expected[item_id] = expected.get(item_id, 0) - count
            if expected.get(item_id, 0) <= 0:
                expected.pop(item_id, None)
            if expected != after_totals:
                raise RuntimeError("inventory quantity conservation violated")

    def _add_quantity(self, work, container, item_id, count, bind, durability,
                      max_stack, preferred=None, exclude=None):
        exclude = set(exclude or ())
        remaining = count

        def add_to(slot):
            nonlocal remaining
            room = slot["max_stack"] - slot["count"]
            if room <= 0:
                return
            amount = min(room, remaining)
            if amount:
                slot["count"] += amount
                self._bump(slot)
                remaining -= amount

        if preferred is not None:
            preferred_slot = work.get(preferred)
            if preferred_slot is None:
                amount = min(remaining, max_stack)
                work[preferred] = self._new_slot(
                    item_id, amount, bind, durability, max_stack)
                remaining -= amount
            else:
                if not self._compatible(
                        preferred_slot, item_id, bind, durability, max_stack):
                    raise StackConflictError(
                        "SLOT_CONFLICT",
                        "destination slot contains an incompatible item")
                if preferred not in exclude:
                    add_to(preferred_slot)

        for index in range(self._capacity(container)):
            key = (container, index)
            slot = work.get(key)
            if slot is None or key in exclude or key == preferred:
                continue
            if self._compatible(slot, item_id, bind, durability, max_stack):
                add_to(slot)
            if not remaining:
                break

        if remaining:
            for index in range(self._capacity(container)):
                key = (container, index)
                if key in work or key in exclude or key == preferred:
                    continue
                amount = min(remaining, max_stack)
                work[key] = self._new_slot(
                    item_id, amount, bind, durability, max_stack)
                remaining -= amount
                if not remaining:
                    break

        if remaining:
            raise StackConflictError(
                "NO_SPACE", "container cannot hold the requested quantity")

    def _remove_quantity(self, work, key, count):
        slot = work.get(key)
        if slot is None:
            raise StackConflictError("EMPTY_SLOT", "source slot is empty")
        if count > slot["count"]:
            raise StackConflictError(
                "INSUFFICIENT_QUANTITY", "source slot has too few items")
        if count == slot["count"]:
            del work[key]
        else:
            slot["count"] -= count
            self._bump(slot)

    def _diff(self, before, after):
        changes = []
        writes = []
        removes = []
        pre = []
        keys = sorted(set(before) | set(after), key=lambda key: (key[0], key[1]))
        for key in keys:
            old = before.get(key)
            new = after.get(key)
            if old == new:
                continue
            container, index = key
            pre.append({
                "container": container,
                "index": index,
                "version": old["version"] if old is not None else None,
            })
            if new is None:
                removes.append({"container": container, "index": index})
            else:
                writes.append({
                    "container": container,
                    "index": index,
                    "slot": dict(new),
                })
            changes.append({
                "container": container,
                "index": index,
                "before": None if old is None else self._slot_view(
                    container, index, old),
                "after": None if new is None else self._slot_view(
                    container, index, new),
            })
        return changes, {
            "pre": pre,
            "writes": writes,
            "removes": removes,
        }

    @staticmethod
    def _ref(container, index):
        return [container, index]

    def _entry(self, op_id, action, item_id=None, count=None, src=None,
               dst=None, container=None, expected_version=None,
               before=None, after=None):
        changes, plan = self._diff(before, after)
        totals_before = self._totals(before)
        totals_after = self._totals(after)
        delta = {}
        for key in set(totals_before) | set(totals_after):
            change = totals_after.get(key, 0) - totals_before.get(key, 0)
            if change:
                delta[key] = change
        entry = {
            "seq": self._next_op,
            "op_number": self._next_op,
            "operation_no": self._next_op,
            "op_id": op_id,
            "operation_id": op_id,
            "op": op_id,
            "action": action,
            "kind": action,
            "type": action,
            "operation": action,
            "status": "committed",
            "committed": True,
            "item_id": item_id,
            "count": count,
            "src": None if src is None else self._ref(*src),
            "dst": None if dst is None else self._ref(*dst),
            "container": container,
            "expected_version": expected_version,
            "changes": changes,
            "delta": delta,
            "totals_before": totals_before,
            "totals_after": totals_after,
            "plan": plan,
        }
        self._next_op += 1
        return entry

    def _known(self, op_id):
        if op_id in self._applied:
            return True
        return any(entry["op_id"] == op_id for entry in self._pending)

    def _commit(self, entry):
        self._journal.append(entry)
        self._applied.add(entry["op_id"])

    def _suspend(self, entry):
        entry["status"] = "pending"
        entry["committed"] = False
        self._journal.append(entry)
        self._pending.append(entry)
        raise StackConflictError(
            "INTERRUPTED", "operation was journaled but not committed")

    def _apply_stored_plan(self, plan):
        checks = []
        for item in plan.get("pre", []):
            key = (self._container(item["container"]), int(item["index"]))
            current = self._slots.get(key)
            checks.append(
                (key, None if current is None else current["version"],
                 item.get("version")))
        for key, actual, expected in checks:
            if actual != expected:
                raise StackConflictError(
                    "STALE_VERSION",
                    "slot version changed before the operation resumed")
        for item in plan.get("writes", []):
            key = (self._container(item["container"]), int(item["index"]))
            self._slots[key] = dict(item["slot"])
        for item in plan.get("removes", []):
            key = (self._container(item["container"]), int(item["index"]))
            self._slots.pop(key, None)

    def _check_expected(self, expected_version, src, dst):
        if expected_version is None:
            return
        checks = {}
        if isinstance(expected_version, dict):
            for key, value in expected_version.items():
                if key == "src":
                    checks[src] = value
                elif key == "dst":
                    checks[dst] = value
                else:
                    checks[self._key(key)] = value
        elif (isinstance(expected_version, (tuple, list))
              and len(expected_version) == 2
              and all(isinstance(value, int)
                      and not isinstance(value, bool)
                      for value in expected_version)):
            checks[src] = expected_version[0]
            if dst is not None:
                checks[dst] = expected_version[1]
        else:
            checks[src] = expected_version
        for key, expected in checks.items():
            slot = self._slots.get(key)
            actual = None if slot is None else slot["version"]
            if expected == 0 and actual is None:
                continue
            if actual != expected:
                raise StackConflictError(
                    "STALE_VERSION",
                    "slot %s:%s has version %s, expected %s" % (
                        key[0], key[1], actual, expected))

    def put(self, container, index, item_id, count, bind=False, durability=100,
            max_stack=99):
        bind = self._validate_meta(
            item_id, count, bind, durability, max_stack)
        container = self._container(container)
        key = self._key((container, index))
        work = {slot_key: self._clone_slot(slot)
                for slot_key, slot in self._slots.items()}
        existing = work.get(key)
        if existing is not None and not self._compatible(
                existing, item_id, bind, durability, max_stack):
            raise StackConflictError(
                "SLOT_CONFLICT", "slot already contains another item")
        self._add_quantity(
            work, container, item_id, count, bind, durability, max_stack,
            preferred=key)
        self._slots = work
        return self.slot(*key)

    def slot(self, container, index):
        key = self._key((container, index))
        slot = self._slots.get(key)
        if slot is None:
            return None
        return self._slot_view(key[0], key[1], slot)

    def slots(self, container):
        container = self._container(container)
        result = []
        for key in sorted(self._slots, key=lambda item: item[1]):
            if key[0] == container:
                result.append(self._slot_view(key[0], key[1],
                                              self._slots[key]))
        return result

    def find_free(self, container, item_id, need, bind=False, durability=100,
                  max_stack=99):
        bind = self._validate_meta(
            item_id, need, bind, durability, max_stack)
        container = self._container(container)
        for index in range(self._capacity(container)):
            slot = self._slots.get((container, index))
            if slot is not None and self._compatible(
                    slot, item_id, bind, durability, max_stack):
                if slot["count"] + need <= slot["max_stack"]:
                    return index
        for index in range(self._capacity(container)):
            if (container, index) not in self._slots:
                return index
        return None

    def move(self, op_id, src, dst, count=None, expected_version=None,
             crash_midway=False):
        if self._known(op_id):
            return False
        src = self._key(src)
        dst = self._key(dst)
        if src == dst:
            raise StackConflictError(
                "INVALID_SLOT", "source and destination are the same slot")
        source = self._slots.get(src)
        if source is None:
            raise StackConflictError("EMPTY_SLOT", "source slot is empty")
        amount = source["count"] if count is None else self._count(count)
        if amount > source["count"]:
            raise StackConflictError(
                "INSUFFICIENT_QUANTITY", "source slot has too few items")
        destination = self._slots.get(dst)
        if destination is not None and not self._compatible(
                destination, source["item_id"], source["bind"],
                source["durability"], source["max_stack"]):
            raise StackConflictError(
                "SLOT_CONFLICT", "destination slot is incompatible")
        self._check_expected(expected_version, src, dst)
        work = {key: self._clone_slot(slot)
                for key, slot in self._slots.items()}
        self._remove_quantity(work, src, amount)
        self._add_quantity(
            work, dst[0], source["item_id"], amount, source["bind"],
            source["durability"], source["max_stack"], preferred=dst,
            exclude={src})
        self._assert_conserved("move", source["item_id"], amount,
                               self._slots, work)
        entry = self._entry(
            op_id, "move", item_id=source["item_id"], count=amount,
            src=src, dst=dst, expected_version=expected_version,
            before=self._slots, after=work)
        if crash_midway:
            self._suspend(entry)
        self._slots = work
        self._commit(entry)
        return True

    def split(self, op_id, src, count, dst):
        if self._known(op_id):
            return False
        src = self._key(src)
        dst = self._key(dst)
        if src == dst:
            raise StackConflictError(
                "INVALID_SLOT", "source and destination are the same slot")
        source = self._slots.get(src)
        if source is None:
            raise StackConflictError("EMPTY_SLOT", "source slot is empty")
        count = self._count(count)
        if count > source["count"]:
            raise StackConflictError(
                "INSUFFICIENT_QUANTITY", "source slot has too few items")
        destination = self._slots.get(dst)
        if destination is not None and not self._compatible(
                destination, source["item_id"], source["bind"],
                source["durability"], source["max_stack"]):
            raise StackConflictError(
                "SLOT_CONFLICT", "destination slot is incompatible")
        work = {key: self._clone_slot(slot)
                for key, slot in self._slots.items()}
        self._remove_quantity(work, src, count)
        self._add_quantity(
            work, dst[0], source["item_id"], count, source["bind"],
            source["durability"], source["max_stack"], preferred=dst,
            exclude={src})
        self._assert_conserved("split", source["item_id"], count,
                               self._slots, work)
        entry = self._entry(
            op_id, "split", item_id=source["item_id"], count=count,
            src=src, dst=dst, before=self._slots, after=work)
        self._slots = work
        self._commit(entry)
        return True

    def merge(self, op_id, src, dst):
        if self._known(op_id):
            return False
        src = self._key(src)
        dst = self._key(dst)
        if src == dst:
            raise StackConflictError(
                "INVALID_SLOT", "source and destination are the same slot")
        source = self._slots.get(src)
        destination = self._slots.get(dst)
        if source is None or destination is None:
            raise StackConflictError("EMPTY_SLOT", "merge needs two stacks")
        if not self._compatible(
                destination, source["item_id"], source["bind"],
                source["durability"], source["max_stack"]):
            raise StackConflictError(
                "SLOT_CONFLICT", "stacks have incompatible attributes")
        work = {key: self._clone_slot(slot)
                for key, slot in self._slots.items()}
        total = source["count"] + destination["count"]
        max_stack = destination["max_stack"]
        dst_count = min(total, max_stack)
        remaining = total - dst_count
        src_count = min(remaining, max_stack)
        remaining -= src_count
        destination_work = work[dst]
        if destination_work["count"] != dst_count:
            destination_work["count"] = dst_count
            self._bump(destination_work)
        if src_count:
            source_work = work[src]
            if source_work["count"] != src_count:
                source_work["count"] = src_count
                self._bump(source_work)
        else:
            work.pop(src, None)
        if remaining:
            self._add_quantity(
                work, dst[0], source["item_id"], remaining, source["bind"],
                source["durability"], max_stack, exclude={src, dst})
        self._assert_conserved(
            "merge", source["item_id"], source["count"], self._slots, work)
        entry = self._entry(
            op_id, "merge", item_id=source["item_id"],
            count=source["count"], src=src, dst=dst,
            before=self._slots, after=work)
        self._slots = work
        self._commit(entry)
        return True

    def destroy(self, op_id, src, count):
        if self._known(op_id):
            return False
        src = self._key(src)
        source = self._slots.get(src)
        if source is None:
            raise StackConflictError("EMPTY_SLOT", "source slot is empty")
        count = self._count(count)
        if count > source["count"]:
            raise StackConflictError(
                "INSUFFICIENT_QUANTITY", "source slot has too few items")
        work = {key: self._clone_slot(slot)
                for key, slot in self._slots.items()}
        self._remove_quantity(work, src, count)
        self._assert_conserved(
            "destroy", source["item_id"], count, self._slots, work)
        entry = self._entry(
            op_id, "destroy", item_id=source["item_id"], count=count,
            src=src, before=self._slots, after=work)
        self._slots = work
        self._commit(entry)
        return True

    def organize(self, op_id, container):
        if self._known(op_id):
            return False
        container = self._container(container)
        work = {key: self._clone_slot(slot)
                for key, slot in self._slots.items()}
        groups = defaultdict(list)
        for index in range(self._capacity(container)):
            key = (container, index)
            slot = work.get(key)
            if slot is None:
                continue
            identity = (
                slot["item_id"], bool(slot["bind"]), slot["durability"],
                slot["max_stack"])
            groups[identity].append(key)
        for identity, keys in groups.items():
            total = sum(work[key]["count"] for key in keys)
            max_stack = identity[3]
            for key in keys:
                if total <= 0:
                    work.pop(key, None)
                    continue
                wanted = min(total, max_stack)
                slot = work[key]
                if slot["count"] != wanted:
                    slot["count"] = wanted
                    self._bump(slot)
                total -= wanted
            if total:
                item_id, bind, durability, _ = identity
                for index in range(self._capacity(container)):
                    key = (container, index)
                    if key in work:
                        continue
                    amount = min(total, max_stack)
                    work[key] = self._new_slot(
                        item_id, amount, bind, durability, max_stack)
                    total -= amount
                    if not total:
                        break
                if total:
                    raise StackConflictError(
                        "NO_SPACE",
                        "organize cannot place all overflow items")
        self._assert_conserved("organize", None, 0, self._slots, work)
        entry = self._entry(
            op_id, "organize", container=container,
            before=self._slots, after=work)
        self._slots = work
        self._commit(entry)
        return True

    def total(self, item_id, include_mail=True):
        total = 0
        for (container, _), slot in self._slots.items():
            if container == "mail" and not include_mail:
                continue
            if item_id is None or slot["item_id"] == item_id:
                total += slot["count"]
        return total

    def sell(self, op_id, container, index, count, unit_price):
        if self._known(op_id):
            return 0
        key = self._key((container, index))
        slot = self._slots.get(key)
        if slot is None:
            raise StackConflictError("EMPTY_SLOT", "source slot is empty")
        count = self._count(count)
        if count > slot["count"]:
            raise StackConflictError(
                "INSUFFICIENT_QUANTITY", "source slot has too few items")
        if (isinstance(unit_price, bool)
                or not isinstance(unit_price, (int, float))
                or unit_price < 0):
            raise StackConflictError(
                "INVALID_ARGUMENT", "unit_price must be non-negative")
        work = {slot_key: self._clone_slot(item)
                for slot_key, item in self._slots.items()}
        self._remove_quantity(work, key, count)
        self._assert_conserved("sell", slot["item_id"], count,
                               self._slots, work)
        entry = self._entry(
            op_id, "sell", item_id=slot["item_id"], count=count,
            src=key, before=self._slots, after=work)
        self._slots = work
        self._commit(entry)
        return count * unit_price

    def extract_mail(self, op_id, mail_index, dest_container):
        if self._known(op_id):
            return False
        src = self._key(("mail", mail_index))
        slot = self._slots.get(src)
        if slot is None:
            raise StackConflictError("EMPTY_SLOT", "mail attachment is empty")
        dest = self._container(dest_container)
        work = {key: self._clone_slot(item)
                for key, item in self._slots.items()}
        self._add_quantity(
            work, dest, slot["item_id"], slot["count"], slot["bind"],
            slot["durability"], slot["max_stack"], exclude={src})
        work.pop(src, None)
        destination_keys = [
            key for key in work
            if key[0] == dest
            and (key not in self._slots or self._slots[key] != work[key])
        ]
        dst_ref = min(destination_keys, key=lambda key: key[1])
        self._assert_conserved(
            "extract_mail", slot["item_id"], slot["count"],
            self._slots, work)
        entry = self._entry(
            op_id, "extract_mail", item_id=slot["item_id"],
            count=slot["count"], src=src, dst=dst_ref,
            container=dest, before=self._slots, after=work)
        self._slots = work
        self._commit(entry)
        return True

    def resume_pending(self):
        resumed = 0
        for entry in list(self._pending):
            if entry["op_id"] in self._applied:
                entry["status"] = "committed"
                entry["committed"] = True
                self._pending.remove(entry)
                continue
            self._apply_stored_plan(entry["plan"])
            entry["status"] = "committed"
            entry["committed"] = True
            self._applied.add(entry["op_id"])
            self._pending.remove(entry)
            resumed += 1
        return bool(resumed)

    def journal(self):
        return deepcopy([
            {key: value for key, value in entry.items()
             if not key.startswith("_")}
            for entry in self._journal
        ])

    def snapshot(self):
        return {
            "bag_size": self.bag_size,
            "warehouse_size": self.warehouse_size,
            "mail_size": self.mail_size,
            "temp_size": self._temp_size,
            "guild_size": self._guild_size,
            "next_op": self._next_op,
            "slots": [
                dict(self._slot_view(container, index, slot))
                for (container, index), slot in sorted(
                    self._slots.items(), key=lambda item: (
                        item[0][0], item[0][1]))
            ],
            "journal": deepcopy(self._journal),
            "pending": [entry["op_id"] for entry in self._pending],
            "applied": sorted(self._applied, key=repr),
        }

    def restore(self, snapshot):
        saved = deepcopy(snapshot)
        self.bag_size = self._positive_int(
            saved.get("bag_size", self.bag_size), "bag_size")
        self.warehouse_size = self._positive_int(
            saved.get("warehouse_size", self.warehouse_size),
            "warehouse_size")
        self.mail_size = self._positive_int(
            saved.get("mail_size", self.mail_size), "mail_size")
        self._temp_size = self._positive_int(
            saved.get("temp_size", self.bag_size), "temp_size")
        self._guild_size = self._positive_int(
            saved.get("guild_size", self.warehouse_size), "guild_size")
        self._slots = {}
        raw_slots = saved.get("slots", [])
        if isinstance(raw_slots, dict):
            for ref, slot in raw_slots.items():
                container, index = self._key(ref)
                self._slots[(container, index)] = self._clone_slot(slot)
        else:
            for item in raw_slots:
                container = self._container(item["container"])
                index = int(item["index"])
                self._key((container, index))
                slot = {
                    "item_id": item["item_id"],
                    "count": item["count"],
                    "bind": bool(item.get("bind", False)),
                    "durability": item.get("durability", 100),
                    "max_stack": item.get("max_stack", 99),
                    "version": int(item.get("version", 1)),
                }
                self._slots[(container, index)] = slot
        self._journal = deepcopy(saved.get("journal", []))
        self._applied = set(saved.get("applied", []))
        if not self._applied:
            self._applied = {
                entry["op_id"] for entry in self._journal
                if entry.get("committed") or entry.get("status") == "committed"
            }
        self._pending = []
        pending = saved.get("pending", [])
        pending_ids = []
        for item in pending:
            if isinstance(item, dict):
                pending_ids.append(item.get("op_id"))
            else:
                pending_ids.append(item)
        for op_id in pending_ids:
            entry = next((item for item in self._journal
                          if item.get("op_id") == op_id
                          and item.get("status") == "pending"), None)
            if entry is not None:
                self._pending.append(entry)
                continue
            entry = next((item for item in self._journal
                          if item.get("op_id") == op_id), None)
            if entry is not None:
                entry["status"] = "pending"
                entry["committed"] = False
                self._pending.append(entry)
                self._applied.discard(op_id)
        self._next_op = int(saved.get("next_op", len(self._journal) + 1))
        return self
