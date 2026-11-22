"""Vehicle assembly, transfer, insurance and garage recovery.

The garage keeps one authoritative location for every physical part. Assembly
changes, insurance settlement and garage movement are recorded as operations
and are only committed while holding the garage lock.
"""

import copy
import threading


SLOT_INCOMPATIBLE = "slot_incompatible"
SLOT_OCCUPIED = "slot_occupied"
SLOT_EMPTY = "slot_empty"
GARAGE_FULL = "garage_full"
VEHICLE_DESTROYED = "vehicle_destroyed"
UNKNOWN_VEHICLE = "unknown_vehicle"
UNKNOWN_PART = "unknown_part"
ALREADY_SETTLED = "already_settled"
VEHICLE_EXISTS = "vehicle_exists"
PART_EXISTS = "part_exists"
PART_UNAVAILABLE = "part_unavailable"
TRANSFER_PENDING = "transfer_pending"
VEHICLE_NOT_IN_GARAGE = "vehicle_not_in_garage"
PRESET_NOT_FOUND = "preset_not_found"
INSURANCE_NOT_AVAILABLE = "insurance_not_available"
INSURANCE_CONFLICT = "insurance_conflict"


class GarageError(Exception):
    def __init__(self, code, message=""):
        self.code = code
        super().__init__(code if not message else "%s: %s" % (code, message))


class SlotIncompatibleError(GarageError):
    pass


class VehicleAssemblyGarage:
    def __init__(self, garage_capacity=10, server_time=0):
        self.garage_capacity = int(garage_capacity)
        self.server_time = int(server_time)
        self._lock = threading.RLock()
        self.parts = {}
        self.vehicles = {}
        self._part_locations = {}
        self._garages = {}
        self._settlements = {}
        self._claims = {}
        self._operations = []
        self._pending_operation_ids = []
        self._next_operation_id = 1
        self._stat_keys = set()

    def register_part(self, part_id, part_type, compatible_models=(),
                      blueprint_min=1, stats=None):
        with self._lock:
            if part_id in self.parts:
                raise GarageError(PART_EXISTS, "part %s already exists" % part_id)
            if isinstance(compatible_models, str):
                compatible_models = (compatible_models,)
            models = tuple(str(model) for model in compatible_models)
            part_stats = copy.deepcopy(stats or {})
            self.parts[part_id] = {
                "part_id": part_id,
                "part_type": str(part_type),
                "compatible_models": models,
                "blueprint_min": int(blueprint_min),
                "stats": part_stats,
            }
            self._part_locations[part_id] = "warehouse"
            self._stat_keys.update(part_stats)
            self._tick()
            return {"status": "registered", "part_id": part_id}

    def register_vehicle(self, vehicle_id, model, blueprint_version, owner_id,
                         garage_id="main"):
        with self._lock:
            if vehicle_id in self.vehicles:
                raise GarageError(VEHICLE_EXISTS,
                                  "vehicle %s already exists" % vehicle_id)
            garage_id = str(garage_id)
            order = self._garages.setdefault(garage_id, [])
            if len(order) >= self.garage_capacity:
                raise GarageError(GARAGE_FULL,
                                  "garage %s is full" % garage_id)
            vehicle = {
                "vehicle_id": vehicle_id,
                "model": str(model),
                "blueprint_version": int(blueprint_version),
                "owner_id": owner_id,
                "garage_id": garage_id,
                "slots": {},
                "destroyed": False,
                "status": "active",
                "insurance_status": "active",
                "livery": None,
                "presets": {},
                "assembly_revision": 0,
                "known_stat_keys": [],
            }
            self.vehicles[vehicle_id] = vehicle
            order.append(vehicle_id)
            self._tick()
            return {"status": "registered", "vehicle_id": vehicle_id,
                    "garage_id": garage_id}

    def install_part(self, vehicle_id, slot, part_id):
        with self._lock:
            vehicle = self._require_vehicle(vehicle_id)
            part = self._require_part(part_id)
            self._assert_mutable(vehicle)
            self._assert_compatible(vehicle, slot, part)
            if slot in vehicle["slots"]:
                raise GarageError(
                    SLOT_OCCUPIED,
                    "slot %s on %s is occupied by %s"
                    % (slot, vehicle_id, vehicle["slots"][slot]))
            location = self._part_locations.get(part_id, "warehouse")
            if location != "warehouse":
                raise GarageError(
                    PART_UNAVAILABLE,
                    "part %s is already at %s" % (part_id, location))

            operation = self._begin_operation(
                "install", vehicle_id=vehicle_id, slot=slot, part_id=part_id)
            self._mark_operation_pending(operation)
            try:
                vehicle["slots"][slot] = part_id
                self._part_locations[part_id] = self._vehicle_location(
                    vehicle_id)
                vehicle["assembly_revision"] += 1
                self._remember_stat_keys(vehicle, part)
            except Exception:
                vehicle["slots"].pop(slot, None)
                self._part_locations[part_id] = "warehouse"
                self._finish_operation(operation, "aborted")
                raise
            self._finish_operation(operation, "committed")
            return {"status": "installed", "vehicle_id": vehicle_id,
                    "slot": slot, "part_id": part_id,
                    "location": self._part_locations[part_id]}

    def remove_part(self, vehicle_id, slot):
        with self._lock:
            vehicle = self._require_vehicle(vehicle_id)
            self._assert_mutable(vehicle)
            part_id = vehicle["slots"].get(slot)
            if part_id is None:
                raise GarageError(SLOT_EMPTY,
                                  "slot %s on %s is empty" % (slot, vehicle_id))
            if part_id not in self.parts:
                raise GarageError(UNKNOWN_PART, "part %s is not registered"
                                  % part_id)

            operation = self._begin_operation(
                "remove", vehicle_id=vehicle_id, slot=slot, part_id=part_id)
            self._mark_operation_pending(operation)
            try:
                del vehicle["slots"][slot]
                self._part_locations[part_id] = "warehouse"
                vehicle["assembly_revision"] += 1
            except Exception:
                vehicle["slots"][slot] = part_id
                self._part_locations[part_id] = self._vehicle_location(
                    vehicle_id)
                self._finish_operation(operation, "aborted")
                raise
            self._finish_operation(operation, "committed")
            return {"status": "removed", "vehicle_id": vehicle_id,
                    "slot": slot, "part_id": part_id,
                    "location": "warehouse"}

    def part_location(self, part_id):
        with self._lock:
            if part_id not in self.parts:
                raise GarageError(UNKNOWN_PART,
                                  "part %s is not registered" % part_id)
            return self._part_locations.get(part_id, "warehouse")

    def garage_vehicles(self, garage_id="main"):
        with self._lock:
            return list(self._garages.get(str(garage_id), ()))

    def transfer(self, vehicle_id, from_garage, to_garage):
        with self._lock:
            vehicle = self._require_vehicle(vehicle_id)
            from_garage = str(from_garage)
            to_garage = str(to_garage)
            if vehicle["garage_id"] != from_garage:
                raise GarageError(
                    VEHICLE_NOT_IN_GARAGE,
                    "vehicle %s is not in %s" % (vehicle_id, from_garage))
            if from_garage == to_garage:
                return {"status": "already_in_garage",
                        "vehicle_id": vehicle_id, "garage_id": to_garage}
            existing = self._pending_transfer_for(vehicle_id)
            if existing is not None:
                if existing.get("from_garage") == from_garage and \
                        existing.get("to_garage") == to_garage:
                    result = self._recover_transfer_operation(existing)
                    if result["status"] == "transferred":
                        return {"status": "transferred",
                                "vehicle_id": vehicle_id,
                                "from_garage": from_garage,
                                "to_garage": to_garage}
                    raise GarageError(
                        result.get("code", GARAGE_FULL),
                        result.get("message", "transfer failed"))
                raise GarageError(
                    TRANSFER_PENDING,
                    "vehicle %s already has a pending transfer"
                    % vehicle_id)
            self._assert_destination_available(vehicle, to_garage)
            operation = self._begin_transfer_operation(
                vehicle, from_garage, to_garage)
            result = self._recover_transfer_operation(operation)
            if result["status"] != "transferred":
                raise GarageError(result.get("code", GARAGE_FULL),
                                  result.get("message", "transfer failed"))
            return {"status": "transferred", "vehicle_id": vehicle_id,
                    "from_garage": from_garage, "to_garage": to_garage}

    def begin_transfer(self, vehicle_id, to_garage):
        with self._lock:
            vehicle = self._require_vehicle(vehicle_id)
            to_garage = str(to_garage)
            from_garage = vehicle["garage_id"]
            if from_garage == to_garage:
                return {"status": "already_in_garage",
                        "vehicle_id": vehicle_id, "garage_id": to_garage}
            existing = self._pending_transfer_for(vehicle_id)
            if existing is not None:
                if existing["to_garage"] == to_garage:
                    return copy.deepcopy(existing)
                raise GarageError(TRANSFER_PENDING,
                                  "vehicle %s already has a pending transfer"
                                  % vehicle_id)
            self._assert_destination_available(vehicle, to_garage)
            operation = self._begin_transfer_operation(
                vehicle, from_garage, to_garage)
            return copy.deepcopy(operation)

    def recover(self):
        with self._lock:
            self._reconcile_locations()
            results = []
            pending_ids = list(self._pending_operation_ids)
            for operation_id in pending_ids:
                operation = self._operation_by_id(operation_id)
                if operation is None or operation.get("state") != "pending":
                    self._drop_pending_operation(operation_id)
                    continue
                kind = operation.get("kind")
                if kind == "transfer":
                    result = self._recover_transfer_operation(operation)
                elif kind == "install":
                    result = self._recover_install_operation(operation)
                elif kind == "remove":
                    result = self._recover_remove_operation(operation)
                elif kind == "apply_preset":
                    result = self._recover_preset_operation(operation)
                else:
                    self._finish_operation(operation, "aborted")
                    result = {"status": "aborted", "operation_id": operation_id}
                results.append(result)
            self._reconcile_locations()
            return results

    def report_destroyed(self, battle_id, vehicle_id, killer_id=None):
        with self._lock:
            vehicle = self._require_vehicle(vehicle_id)
            battle = self._settlements.get(battle_id)
            existing = battle.get(vehicle_id) if battle is not None else None
            if existing is not None:
                return {"status": "already_settled",
                        "settlement": copy.deepcopy(existing)}
            if vehicle.get("destroyed"):
                previous = self._latest_settlement(vehicle_id)
                return {"status": "already_settled",
                        "settlement": copy.deepcopy(previous)}

            settlement = {
                "battle_id": battle_id,
                "vehicle_id": vehicle_id,
                "killer_id": killer_id,
                "owner_id": vehicle["owner_id"],
                "model": vehicle["model"],
                "blueprint_version": vehicle["blueprint_version"],
                "garage_id": vehicle["garage_id"],
                "slots": copy.deepcopy(vehicle["slots"]),
                "parts": [
                    vehicle["slots"][slot]
                    for slot in sorted(vehicle["slots"], key=str)
                ],
                "livery": vehicle["livery"],
                "status": "settled",
                "claimed": False,
                "settled_at": self._tick(),
            }
            self._settlements.setdefault(
                battle_id, {})[vehicle_id] = settlement
            vehicle["destroyed"] = True
            vehicle["status"] = "destroyed"
            vehicle["insurance_status"] = "claimable"
            return {"status": "destroyed",
                    "settlement": copy.deepcopy(settlement)}

    def claim_insurance(self, vehicle_id, battle_id):
        with self._lock:
            vehicle = self._require_vehicle(vehicle_id)
            battle = self._settlements.get(battle_id, {})
            settlement = battle.get(vehicle_id)
            if settlement is None:
                raise GarageError(
                    INSURANCE_NOT_AVAILABLE,
                    "no destruction settlement for %s in battle %s"
                    % (vehicle_id, battle_id))
            existing_claim = self._claims.get(battle_id, {}).get(vehicle_id)
            if existing_claim is not None:
                return {"status": "already_claimed",
                        "claim": copy.deepcopy(existing_claim),
                        "restored_parts": list(
                            existing_claim.get("restored_parts", ()))}

            desired_slots = copy.deepcopy(settlement["slots"])
            self._validate_desired_slots(vehicle, desired_slots)
            self._set_assembly(vehicle, desired_slots)
            if settlement.get("livery") is not None:
                vehicle["livery"] = settlement["livery"]
            vehicle["destroyed"] = False
            vehicle["status"] = "active"
            vehicle["insurance_status"] = "settled"

            restored_parts = [
                desired_slots[slot] for slot in sorted(desired_slots)]
            claim = {
                "claim_id": "claim-%s-%s" % (battle_id, vehicle_id),
                "battle_id": battle_id,
                "vehicle_id": vehicle_id,
                "owner_id": settlement.get("owner_id"),
                "model": settlement.get("model"),
                "blueprint_version": settlement.get("blueprint_version"),
                "garage_id": settlement.get("garage_id"),
                "slots": copy.deepcopy(desired_slots),
                "parts": list(restored_parts),
                "restored_parts": list(restored_parts),
                "livery": vehicle["livery"],
                "status": "claimed",
                "claimed_at": self._tick(),
            }
            self._claims.setdefault(battle_id, {})[vehicle_id] = claim
            settlement["claimed"] = True
            settlement["claimed_at"] = claim["claimed_at"]
            settlement["claim_id"] = claim["claim_id"]
            return {"status": "claimed", "claim": copy.deepcopy(claim),
                    "parts": list(restored_parts),
                    "restored_parts": list(restored_parts)}

    def combat_snapshot(self, vehicle_id):
        with self._lock:
            vehicle = self._require_vehicle(vehicle_id)
            stats = {key: 0 for key in vehicle["known_stat_keys"]}
            for slot in sorted(vehicle["slots"], key=str):
                part_id = vehicle["slots"][slot]
                part = self.parts.get(part_id)
                if part is None:
                    continue
                for key, value in part["stats"].items():
                    if isinstance(value, (int, float)) and not isinstance(
                            value, bool):
                        stats[key] = stats.get(key, 0) + value
            power = sum(
                value for value in stats.values()
                if isinstance(value, (int, float)) and not isinstance(
                    value, bool))
            return {
                "vehicle_id": vehicle_id,
                "model": vehicle["model"],
                "blueprint_version": vehicle["blueprint_version"],
                "owner_id": vehicle["owner_id"],
                "garage_id": vehicle["garage_id"],
                "status": vehicle["status"],
                "insurance_status": vehicle["insurance_status"],
                "slots": copy.deepcopy(vehicle["slots"]),
                "stats": stats,
                "power": power,
                "livery": vehicle["livery"],
                "assembly_revision": vehicle["assembly_revision"],
            }

    def combat_power(self, vehicle_id):
        return self.combat_snapshot(vehicle_id)["power"]

    def combat_stats(self, vehicle_id):
        return self.combat_snapshot(vehicle_id)["stats"]

    def can_drive(self, vehicle_id):
        with self._lock:
            vehicle = self._require_vehicle(vehicle_id)
            return not vehicle.get("destroyed", False) and \
                vehicle.get("status") != "destroyed"

    def can_claim_insurance(self, vehicle_id):
        with self._lock:
            vehicle = self._require_vehicle(vehicle_id)
            settlement = self._latest_settlement(vehicle_id)
            return bool(vehicle.get("destroyed") and settlement and
                        not settlement.get("claimed"))

    def insurance_record(self, vehicle_id, battle_id):
        with self._lock:
            self._require_vehicle(vehicle_id)
            settlement = self._settlements.get(battle_id, {}).get(vehicle_id)
            if settlement is None:
                raise GarageError(
                    INSURANCE_NOT_AVAILABLE,
                    "no destruction settlement for %s in battle %s"
                    % (vehicle_id, battle_id))
            claim = self._claims.get(battle_id, {}).get(vehicle_id)
            return {
                "settlement": copy.deepcopy(settlement),
                "claim": copy.deepcopy(claim),
            }

    def kill_count(self, battle_id):
        with self._lock:
            return len(self._settlements.get(battle_id, {}))

    def save_preset(self, vehicle_id, name):
        with self._lock:
            vehicle = self._require_vehicle(vehicle_id)
            self._assert_mutable(vehicle)
            vehicle["presets"][name] = {
                "name": name,
                "slots": copy.deepcopy(vehicle["slots"]),
                "blueprint_version": vehicle["blueprint_version"],
                "saved_at": self._tick(),
            }
            return {"status": "saved", "vehicle_id": vehicle_id, "name": name,
                    "slots": copy.deepcopy(vehicle["slots"])}

    def apply_preset(self, vehicle_id, name):
        with self._lock:
            vehicle = self._require_vehicle(vehicle_id)
            self._assert_mutable(vehicle)
            preset = vehicle["presets"].get(name)
            if preset is None:
                raise GarageError(PRESET_NOT_FOUND,
                                  "preset %s does not exist on %s"
                                  % (name, vehicle_id))
            desired_slots = copy.deepcopy(preset["slots"])
            self._validate_desired_slots(vehicle, desired_slots)
            operation = self._begin_operation(
                "apply_preset", vehicle_id=vehicle_id, preset=name,
                before_slots=copy.deepcopy(vehicle["slots"]),
                after_slots=copy.deepcopy(desired_slots))
            self._mark_operation_pending(operation)
            try:
                self._set_assembly(vehicle, desired_slots)
            except Exception:
                self._set_assembly(vehicle, operation["before_slots"])
                self._finish_operation(operation, "aborted")
                raise
            self._finish_operation(operation, "committed")
            return {"status": "applied", "vehicle_id": vehicle_id,
                    "name": name, "slots": copy.deepcopy(vehicle["slots"])}

    def apply_livery(self, vehicle_id, livery_id):
        with self._lock:
            vehicle = self._require_vehicle(vehicle_id)
            self._assert_mutable(vehicle)
            vehicle["livery"] = livery_id
            self._tick()
            return {"status": "applied", "vehicle_id": vehicle_id,
                    "livery": livery_id}

    def season_reset(self):
        with self._lock:
            preserved = {
                vehicle_id: vehicle["livery"]
                for vehicle_id, vehicle in self.vehicles.items()
                if vehicle.get("livery") is not None
            }
            self._tick()
            return {"status": "reset", "preserved_liveries": preserved}

    def update_blueprint(self, vehicle_id, blueprint_version):
        with self._lock:
            vehicle = self._require_vehicle(vehicle_id)
            self._assert_mutable(vehicle)
            new_version = int(blueprint_version)
            for slot, part_id in vehicle["slots"].items():
                part = self._require_part(part_id)
                self._assert_compatible(vehicle, slot, part, new_version)
            vehicle["blueprint_version"] = new_version
            vehicle["assembly_revision"] += 1
            self._tick()
            return {"status": "updated", "vehicle_id": vehicle_id,
                    "blueprint_version": new_version,
                    "slots": copy.deepcopy(vehicle["slots"])}

    def upgrade_blueprint(self, vehicle_id, blueprint_version):
        return self.update_blueprint(vehicle_id, blueprint_version)

    def set_blueprint_version(self, vehicle_id, blueprint_version):
        return self.update_blueprint(vehicle_id, blueprint_version)

    def snapshot(self):
        with self._lock:
            pending = []
            for operation_id in self._pending_operation_ids:
                operation = self._operation_by_id(operation_id)
                if operation is not None:
                    pending.append(copy.deepcopy(operation))
            return {
                "schema_version": 1,
                "garage_capacity": self.garage_capacity,
                "server_time": self.server_time,
                "parts": copy.deepcopy(self.parts),
                "vehicles": copy.deepcopy(self.vehicles),
                "part_locations": copy.deepcopy(self._part_locations),
                "garages": copy.deepcopy(self._garages),
                "settlements": copy.deepcopy(self._settlements),
                "insurance_claims": copy.deepcopy(self._claims),
                "operations": copy.deepcopy(self._operations),
                "pending_operations": pending,
            }

    def restore(self, snapshot):
        if not isinstance(snapshot, dict):
            raise ValueError("snapshot must be a dictionary")
        data = copy.deepcopy(snapshot)
        with self._lock:
            self.garage_capacity = max(
                1, int(data.get("garage_capacity", self.garage_capacity)))
            self.server_time = int(data.get("server_time", 0))
            self.parts = copy.deepcopy(data.get("parts", {}))
            self.vehicles = copy.deepcopy(data.get("vehicles", {}))
            self._settlements = copy.deepcopy(data.get("settlements", {}))
            self._claims = copy.deepcopy(data.get("insurance_claims", {}))
            self._operations = copy.deepcopy(data.get("operations", []))
            self._pending_operation_ids = []
            self._stat_keys = set()

            for part_id, part in self.parts.items():
                part.setdefault("part_id", part_id)
                part.setdefault("part_type", "")
                models = part.get("compatible_models", ())
                if isinstance(models, str):
                    models = (models,)
                part["compatible_models"] = tuple(models)
                part.setdefault("blueprint_min", 1)
                part.setdefault("stats", {})
                self._stat_keys.update(part["stats"])

            for vehicle_id, vehicle in self.vehicles.items():
                vehicle.setdefault("vehicle_id", vehicle_id)
                vehicle.setdefault("model", "")
                vehicle.setdefault("blueprint_version", 1)
                vehicle.setdefault("owner_id", None)
                vehicle.setdefault("garage_id", "main")
                vehicle.setdefault("slots", {})
                vehicle.setdefault("presets", {})
                vehicle.setdefault("livery", None)
                vehicle.setdefault("assembly_revision", 0)
                vehicle.setdefault("known_stat_keys", [])
                destroyed = bool(vehicle.get("destroyed", False)) or \
                    vehicle.get("status") == "destroyed"
                vehicle["destroyed"] = destroyed
                vehicle["status"] = "destroyed" if destroyed else "active"
                vehicle.setdefault(
                    "insurance_status",
                    "claimable" if destroyed else "active")

            self._garages = {}
            raw_garages = data.get("garages", {})
            for garage_id, vehicle_ids in raw_garages.items():
                garage_id = str(garage_id)
                order = self._garages.setdefault(garage_id, [])
                for vehicle_id in vehicle_ids:
                    if vehicle_id in self.vehicles and \
                            vehicle_id not in order:
                        order.append(vehicle_id)
            for vehicle_id, vehicle in self.vehicles.items():
                garage_id = str(vehicle["garage_id"])
                vehicle["garage_id"] = garage_id
                order = self._garages.setdefault(garage_id, [])
                if vehicle_id not in order:
                    order.append(vehicle_id)

            self._operations = [
                operation for operation in self._operations
                if isinstance(operation, dict) and operation.get("op_id")
            ]
            operation_ids = {
                operation["op_id"]: operation
                for operation in self._operations
            }
            pending = data.get("pending_operations", [])
            for operation in pending:
                if not isinstance(operation, dict):
                    continue
                operation_id = operation.get("op_id")
                if operation_id is None:
                    continue
                if operation_id not in operation_ids:
                    operation["state"] = "pending"
                    self._operations.append(operation)
                    operation_ids[operation_id] = operation
                self._pending_operation_ids.append(operation_id)
            for operation in self._operations:
                if operation.get("state") == "pending" and \
                        operation["op_id"] not in self._pending_operation_ids:
                    self._pending_operation_ids.append(operation["op_id"])

            next_id = 1
            for operation in self._operations:
                operation_id = str(operation.get("op_id", ""))
                if operation_id.startswith("op-"):
                    try:
                        next_id = max(next_id, int(operation_id[3:]) + 1)
                    except ValueError:
                        pass
            self._next_operation_id = next_id
            self._reconcile_locations()

    def _require_vehicle(self, vehicle_id):
        vehicle = self.vehicles.get(vehicle_id)
        if vehicle is None:
            raise GarageError(UNKNOWN_VEHICLE,
                              "vehicle %s is not registered" % vehicle_id)
        return vehicle

    def _require_part(self, part_id):
        part = self.parts.get(part_id)
        if part is None:
            raise GarageError(UNKNOWN_PART,
                              "part %s is not registered" % part_id)
        return part

    def _assert_mutable(self, vehicle):
        if vehicle.get("destroyed") or vehicle.get("status") == "destroyed":
            raise GarageError(VEHICLE_DESTROYED,
                              "vehicle %s is destroyed"
                              % vehicle["vehicle_id"])

    def _assert_compatible(self, vehicle, slot, part,
                           blueprint_version=None):
        version = vehicle["blueprint_version"] \
            if blueprint_version is None else blueprint_version
        if str(slot) != part["part_type"]:
            raise SlotIncompatibleError(
                SLOT_INCOMPATIBLE,
                "%s cannot be installed in slot %s" % (part["part_id"], slot))
        models = part.get("compatible_models", ())
        if models and vehicle["model"] not in models and "*" not in models:
            raise SlotIncompatibleError(
                SLOT_INCOMPATIBLE,
                "%s is incompatible with model %s"
                % (part["part_type"], vehicle["model"]))
        if int(version) < int(part.get("blueprint_min", 1)):
            raise SlotIncompatibleError(
                SLOT_INCOMPATIBLE,
                "%s requires blueprint %s, vehicle %s has %s"
                % (part["part_type"], part.get("blueprint_min"),
                   vehicle["vehicle_id"], version))

    def _validate_desired_slots(self, vehicle, desired_slots):
        if not isinstance(desired_slots, dict):
            raise GarageError(SLOT_INCOMPATIBLE,
                              "preset slots must be a dictionary")
        seen = set()
        vehicle_location = self._vehicle_location(vehicle["vehicle_id"])
        for slot, part_id in desired_slots.items():
            if part_id in seen:
                raise SlotIncompatibleError(
                    SLOT_INCOMPATIBLE,
                    "part %s appears in more than one slot" % part_id)
            seen.add(part_id)
            part = self._require_part(part_id)
            self._assert_compatible(vehicle, slot, part)
            location = self._part_locations.get(part_id, "warehouse")
            if location not in ("warehouse", vehicle_location):
                raise GarageError(
                    PART_UNAVAILABLE,
                    "part %s is already at %s" % (part_id, location))

    def _set_assembly(self, vehicle, desired_slots):
        vehicle_id = vehicle["vehicle_id"]
        before_slots = copy.deepcopy(vehicle["slots"])
        before_stat_keys = copy.deepcopy(vehicle.get("known_stat_keys", []))
        before_locations = {
            part_id: self._part_locations.get(part_id, "warehouse")
            for part_id in set(before_slots.values()) | set(
                desired_slots.values())
        }
        try:
            for part_id in before_slots.values():
                if part_id in self._part_locations:
                    self._part_locations[part_id] = "warehouse"
            for slot, part_id in desired_slots.items():
                self._part_locations[part_id] = self._vehicle_location(
                    vehicle_id)
            vehicle["slots"] = copy.deepcopy(desired_slots)
            for part_id in desired_slots.values():
                part = self.parts.get(part_id)
                if part is not None:
                    self._remember_stat_keys(vehicle, part)
            if before_slots != desired_slots:
                vehicle["assembly_revision"] += 1
        except Exception:
            vehicle["slots"] = before_slots
            vehicle["known_stat_keys"] = before_stat_keys
            for part_id, location in before_locations.items():
                if part_id in self._part_locations:
                    self._part_locations[part_id] = location
            raise

    def _assert_destination_available(self, vehicle, to_garage):
        order = self._garages.setdefault(to_garage, [])
        if vehicle["vehicle_id"] not in order and \
                len(order) >= self.garage_capacity:
            raise GarageError(GARAGE_FULL,
                              "garage %s is full" % to_garage)

    def _begin_operation(self, kind, **details):
        operation_id = "op-%s" % self._next_operation_id
        self._next_operation_id += 1
        operation = {
            "op_id": operation_id,
            "kind": kind,
            "state": "prepared",
            "created_at": self._tick(),
        }
        operation.update(details)
        self._operations.append(operation)
        return operation

    def _begin_transfer_operation(self, vehicle, from_garage, to_garage):
        operation = self._begin_operation(
            "transfer", vehicle_id=vehicle["vehicle_id"],
            from_garage=from_garage, to_garage=to_garage)
        self._mark_operation_pending(operation)
        return operation

    def _mark_operation_pending(self, operation):
        operation["state"] = "pending"
        operation["status"] = "pending"
        if operation["op_id"] not in self._pending_operation_ids:
            self._pending_operation_ids.append(operation["op_id"])

    def _finish_operation(self, operation, state, **details):
        operation["state"] = state
        operation["status"] = state
        operation["finished_at"] = self._tick()
        operation.update(details)
        self._drop_pending_operation(operation["op_id"])
        return operation

    def _drop_pending_operation(self, operation_id):
        if operation_id in self._pending_operation_ids:
            self._pending_operation_ids.remove(operation_id)

    def _operation_by_id(self, operation_id):
        for operation in self._operations:
            if operation.get("op_id") == operation_id:
                return operation
        return None

    def _pending_transfer_for(self, vehicle_id):
        for operation_id in self._pending_operation_ids:
            operation = self._operation_by_id(operation_id)
            if operation is not None and \
                    operation.get("kind") == "transfer" and \
                    operation.get("state") == "pending" and \
                    operation.get("vehicle_id") == vehicle_id:
                return operation
        return None

    def _recover_transfer_operation(self, operation):
        vehicle = self.vehicles.get(operation.get("vehicle_id"))
        if vehicle is None:
            self._finish_operation(operation, "aborted",
                                   error=UNKNOWN_VEHICLE)
            return {"status": "aborted", "code": UNKNOWN_VEHICLE,
                    "operation_id": operation["op_id"]}

        from_garage = str(operation["from_garage"])
        to_garage = str(operation["to_garage"])
        vehicle_id = vehicle["vehicle_id"]
        destination = self._garages.setdefault(to_garage, [])
        self._garages.setdefault(from_garage, [])

        if vehicle["garage_id"] == to_garage and vehicle_id in destination:
            self._finish_operation(operation, "committed")
            return {"status": "transferred", "vehicle_id": vehicle_id,
                    "garage_id": to_garage,
                    "operation_id": operation["op_id"]}

        if vehicle_id not in destination and \
                len(destination) >= self.garage_capacity:
            operation["last_error"] = GARAGE_FULL
            operation["state"] = "pending"
            return {"status": "blocked", "code": GARAGE_FULL,
                    "vehicle_id": vehicle_id, "garage_id": to_garage,
                    "operation_id": operation["op_id"]}

        for garage_order in self._garages.values():
            while vehicle_id in garage_order:
                garage_order.remove(vehicle_id)
        destination.append(vehicle_id)
        vehicle["garage_id"] = to_garage
        self._finish_operation(operation, "committed")
        return {"status": "transferred", "vehicle_id": vehicle_id,
                "garage_id": to_garage,
                "operation_id": operation["op_id"]}

    def _recover_install_operation(self, operation):
        vehicle = self.vehicles.get(operation.get("vehicle_id"))
        part = self.parts.get(operation.get("part_id"))
        slot = operation.get("slot")
        if vehicle is None or part is None:
            self._finish_operation(operation, "aborted")
            return {"status": "aborted", "operation_id": operation["op_id"]}
        if vehicle["slots"].get(slot) == part["part_id"]:
            self._part_locations[part["part_id"]] = self._vehicle_location(
                vehicle["vehicle_id"])
            self._finish_operation(operation, "committed")
            return {"status": "installed", "vehicle_id": vehicle["vehicle_id"],
                    "slot": slot, "part_id": part["part_id"],
                    "operation_id": operation["op_id"]}
        if vehicle.get("destroyed") or slot in vehicle["slots"] or \
                self._part_locations.get(part["part_id"]) != "warehouse":
            self._finish_operation(operation, "aborted")
            return {"status": "aborted", "operation_id": operation["op_id"]}
        try:
            self._assert_compatible(vehicle, slot, part)
        except GarageError:
            self._finish_operation(operation, "aborted")
            return {"status": "aborted", "operation_id": operation["op_id"]}
        vehicle["slots"][slot] = part["part_id"]
        self._part_locations[part["part_id"]] = self._vehicle_location(
            vehicle["vehicle_id"])
        vehicle["assembly_revision"] += 1
        self._finish_operation(operation, "committed")
        return {"status": "installed", "vehicle_id": vehicle["vehicle_id"],
                "slot": slot, "part_id": part["part_id"],
                "operation_id": operation["op_id"]}

    def _recover_remove_operation(self, operation):
        vehicle = self.vehicles.get(operation.get("vehicle_id"))
        slot = operation.get("slot")
        part_id = operation.get("part_id")
        if vehicle is None or part_id not in self.parts:
            self._finish_operation(operation, "aborted")
            return {"status": "aborted", "operation_id": operation["op_id"]}
        if vehicle["slots"].get(slot) == part_id:
            del vehicle["slots"][slot]
            vehicle["assembly_revision"] += 1
        self._part_locations[part_id] = "warehouse"
        self._finish_operation(operation, "committed")
        return {"status": "removed", "vehicle_id": vehicle["vehicle_id"],
                "slot": slot, "part_id": part_id,
                "operation_id": operation["op_id"]}

    def _recover_preset_operation(self, operation):
        vehicle = self.vehicles.get(operation.get("vehicle_id"))
        desired = operation.get("after_slots")
        if vehicle is None or not isinstance(desired, dict):
            self._finish_operation(operation, "aborted")
            return {"status": "aborted", "operation_id": operation["op_id"]}
        if vehicle.get("slots") == desired:
            self._finish_operation(operation, "committed")
            return {"status": "applied", "vehicle_id": vehicle["vehicle_id"],
                    "operation_id": operation["op_id"]}
        if vehicle.get("destroyed"):
            self._finish_operation(operation, "aborted")
            return {"status": "aborted", "operation_id": operation["op_id"]}
        try:
            self._validate_desired_slots(vehicle, desired)
            self._set_assembly(vehicle, desired)
        except GarageError:
            self._finish_operation(operation, "aborted")
            return {"status": "aborted", "operation_id": operation["op_id"]}
        self._finish_operation(operation, "committed")
        return {"status": "applied", "vehicle_id": vehicle["vehicle_id"],
                "operation_id": operation["op_id"]}

    def _reconcile_locations(self):
        owner = {}
        self._part_locations = {
            part_id: "warehouse" for part_id in self.parts
        }
        ordered_vehicles = []
        for order in self._garages.values():
            for vehicle_id in order:
                if vehicle_id in self.vehicles and \
                        vehicle_id not in ordered_vehicles:
                    ordered_vehicles.append(vehicle_id)
        for vehicle_id in self.vehicles:
            if vehicle_id not in ordered_vehicles:
                ordered_vehicles.append(vehicle_id)

        for vehicle_id in ordered_vehicles:
            vehicle = self.vehicles[vehicle_id]
            slots = vehicle.get("slots", {})
            if not isinstance(slots, dict):
                slots = {}
            clean_slots = {}
            for slot in sorted(slots, key=str):
                part_id = slots[slot]
                if part_id not in self.parts or part_id in owner:
                    continue
                clean_slots[slot] = part_id
                owner[part_id] = vehicle_id
            vehicle["slots"] = clean_slots
            for part_id in clean_slots.values():
                self._part_locations[part_id] = self._vehicle_location(
                    vehicle_id)

        seen_memberships = set()
        for garage_id in list(self._garages):
            cleaned = []
            for vehicle_id in self._garages[garage_id]:
                if vehicle_id in self.vehicles and \
                        vehicle_id not in seen_memberships:
                    cleaned.append(vehicle_id)
                    seen_memberships.add(vehicle_id)
                    self.vehicles[vehicle_id]["garage_id"] = garage_id
            self._garages[garage_id] = cleaned
        for vehicle_id, vehicle in self.vehicles.items():
            if vehicle_id in seen_memberships:
                continue
            garage_id = str(vehicle.get("garage_id", "main"))
            vehicle["garage_id"] = garage_id
            self._garages.setdefault(garage_id, []).append(vehicle_id)
            seen_memberships.add(vehicle_id)
        for vehicle in self.vehicles.values():
            known = set(vehicle.get("known_stat_keys", ()))
            for part_id in vehicle.get("slots", {}).values():
                part = self.parts.get(part_id)
                if part is not None:
                    known.update(part.get("stats", {}))
            vehicle["known_stat_keys"] = sorted(known)

    def _latest_settlement(self, vehicle_id):
        latest = None
        for battle in self._settlements.values():
            settlement = battle.get(vehicle_id)
            if settlement is None:
                continue
            if latest is None or settlement.get(
                    "settled_at", 0) >= latest.get("settled_at", 0):
                latest = settlement
        return latest

    @staticmethod
    def _vehicle_location(vehicle_id):
        return "vehicle:%s" % vehicle_id

    @staticmethod
    def _remember_stat_keys(vehicle, part):
        known = set(vehicle.get("known_stat_keys", ()))
        known.update(part.get("stats", {}))
        vehicle["known_stat_keys"] = sorted(known)

    def _tick(self):
        self.server_time += 1
        return self.server_time
