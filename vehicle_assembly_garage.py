"""Vehicle assembly, transfer, insurance and garage recovery (skeleton).

Slot compatibility checks model and blueprint version together, install and
remove are atomic, destruction settlement is idempotent and snapshots keep
one physical part in exactly one place.  No behaviour is implemented yet.
"""

SLOT_INCOMPATIBLE = "slot_incompatible"
SLOT_OCCUPIED = "slot_occupied"
SLOT_EMPTY = "slot_empty"
GARAGE_FULL = "garage_full"
VEHICLE_DESTROYED = "vehicle_destroyed"
UNKNOWN_VEHICLE = "unknown_vehicle"
UNKNOWN_PART = "unknown_part"
ALREADY_SETTLED = "already_settled"


class GarageError(Exception):
    def __init__(self, code, message=""):
        self.code = code
        super().__init__(code if not message else "%s: %s" % (code, message))


class SlotIncompatibleError(GarageError):
    pass


class VehicleAssemblyGarage:
    def __init__(self, garage_capacity=10, server_time=0):
        self.garage_capacity = garage_capacity
        self.server_time = server_time

    def register_part(self, part_id, part_type, compatible_models=(),
                      blueprint_min=1, stats=None):
        raise NotImplementedError

    def register_vehicle(self, vehicle_id, model, blueprint_version, owner_id,
                         garage_id="main"):
        raise NotImplementedError

    def install_part(self, vehicle_id, slot, part_id):
        raise NotImplementedError

    def remove_part(self, vehicle_id, slot):
        raise NotImplementedError

    def part_location(self, part_id):
        raise NotImplementedError

    def garage_vehicles(self, garage_id="main"):
        raise NotImplementedError

    def transfer(self, vehicle_id, from_garage, to_garage):
        raise NotImplementedError

    def begin_transfer(self, vehicle_id, to_garage):
        raise NotImplementedError

    def recover(self):
        raise NotImplementedError

    def report_destroyed(self, battle_id, vehicle_id, killer_id=None):
        raise NotImplementedError

    def claim_insurance(self, vehicle_id, battle_id):
        raise NotImplementedError

    def combat_snapshot(self, vehicle_id):
        raise NotImplementedError

    def can_drive(self, vehicle_id):
        raise NotImplementedError

    def kill_count(self, battle_id):
        raise NotImplementedError

    def save_preset(self, vehicle_id, name):
        raise NotImplementedError

    def apply_preset(self, vehicle_id, name):
        raise NotImplementedError

    def apply_livery(self, vehicle_id, livery_id):
        raise NotImplementedError

    def season_reset(self):
        raise NotImplementedError

    def snapshot(self):
        raise NotImplementedError

    def restore(self, snapshot):
        raise NotImplementedError
