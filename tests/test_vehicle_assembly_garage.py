import pytest

import vehicle_assembly_garage
from vehicle_assembly_garage import (
    GarageError,
    SlotIncompatibleError,
    VehicleAssemblyGarage,
)


def _game():
    game = VehicleAssemblyGarage(garage_capacity=5)
    game.register_vehicle("bike-1", "bike", 1, "owner")
    return game


def test_tank_turret_cannot_go_on_bike():
    game = _game()
    game.register_part("turret-1", "turret", compatible_models=("tank",),
                       stats={"attack": 50})
    with pytest.raises(SlotIncompatibleError) as exc:
        game.install_part("bike-1", "turret", "turret-1")
    assert exc.value.code == "slot_incompatible"
    assert "turret" in str(exc.value)
    assert game.combat_snapshot("bike-1")["slots"] == {}


def test_blueprint_version_is_checked_together_with_model():
    game = _game()
    game.register_vehicle("car-1", "car", 1, "owner")
    game.register_part("susp-1", "suspension", compatible_models=("car",),
                       blueprint_min=3)
    with pytest.raises(SlotIncompatibleError) as exc:
        game.install_part("car-1", "suspension", "susp-1")
    assert exc.value.code == "slot_incompatible"
    assert game.combat_snapshot("car-1")["slots"] == {}


def test_same_engine_cannot_be_removed_twice():
    game = _game()
    game.register_part("engine-1", "engine", compatible_models=("bike",))
    game.install_part("bike-1", "engine", "engine-1")
    removed = game.remove_part("bike-1", "engine")
    assert removed["status"] == "removed"
    with pytest.raises(GarageError) as exc:
        game.remove_part("bike-1", "engine")
    assert exc.value.code == "slot_empty"
    assert game.part_location("engine-1").startswith("warehouse")


def test_second_part_in_same_slot_does_not_stack():
    game = _game()
    game.register_part("engine-1", "engine", compatible_models=("bike",),
                       stats={"power": 10})
    game.register_part("engine-2", "engine", compatible_models=("bike",),
                       stats={"power": 20})
    game.install_part("bike-1", "engine", "engine-1")
    with pytest.raises(GarageError) as exc:
        game.install_part("bike-1", "engine", "engine-2")
    assert exc.value.code == "slot_occupied"
    snapshot = game.combat_snapshot("bike-1")
    assert snapshot["slots"] == {"engine": "engine-1"}
    assert snapshot["stats"]["power"] == 10


def test_destroyed_settlement_is_idempotent():
    game = _game()
    first = game.report_destroyed("battle-1", "bike-1", "killer-1")
    second = game.report_destroyed("battle-1", "bike-1", "killer-1")
    assert first["status"] == "destroyed"
    assert second["status"] == "already_settled"
    assert game.kill_count("battle-1") == 1
    assert game.combat_snapshot("bike-1")["status"] == "destroyed"


def test_insurance_restores_installed_parts():
    game = _game()
    game.register_part("engine-1", "engine", compatible_models=("bike",))
    game.install_part("bike-1", "engine", "engine-1")
    game.report_destroyed("battle-1", "bike-1", "killer-1")
    claim = game.claim_insurance("bike-1", "battle-1")
    assert claim["status"] == "claimed"
    assert claim["restored_parts"] == ["engine-1"]
    assert game.can_drive("bike-1") is True
    assert game.part_location("engine-1") == "vehicle:bike-1"
    again = game.claim_insurance("bike-1", "battle-1")
    assert again["status"] == "already_claimed"


def test_full_garage_rejects_new_vehicle_without_overwrite():
    game = VehicleAssemblyGarage(garage_capacity=1)
    game.register_vehicle("v1", "car", 1, "owner")
    with pytest.raises(GarageError) as exc:
        game.register_vehicle("v2", "car", 1, "owner")
    assert exc.value.code == "garage_full"
    assert game.garage_vehicles("main") == ["v1"]


def test_interrupted_transfer_recovers_to_exactly_one_garage():
    game = _game()
    transfer = game.begin_transfer("bike-1", "garage-b")
    assert transfer["status"] == "pending"
    snapshot = game.snapshot()

    restored = VehicleAssemblyGarage()
    restored.restore(snapshot)
    restored.recover()
    assert restored.garage_vehicles("garage-b") == ["bike-1"]
    assert restored.garage_vehicles("main") == []


def test_restart_keeps_part_on_one_vehicle_only():
    game = _game()
    game.register_vehicle("bike-2", "bike", 1, "owner")
    game.register_part("engine-1", "engine", compatible_models=("bike",))
    game.install_part("bike-1", "engine", "engine-1")
    snapshot = game.snapshot()

    restored = VehicleAssemblyGarage()
    restored.restore(snapshot)
    assert restored.part_location("engine-1") == "vehicle:bike-1"
    assert restored.combat_snapshot("bike-1")["slots"] == {"engine": "engine-1"}
    assert restored.combat_snapshot("bike-2")["slots"] == {}


def test_combat_snapshot_reflects_removed_part():
    game = _game()
    game.register_part("armor-1", "armor", compatible_models=("bike",),
                       stats={"armor": 10})
    game.install_part("bike-1", "armor", "armor-1")
    assert game.combat_snapshot("bike-1")["stats"]["armor"] == 10
    game.remove_part("bike-1", "armor")
    assert game.combat_snapshot("bike-1")["stats"]["armor"] == 0


def test_destroyed_wreck_cannot_be_modified_or_driven():
    game = _game()
    game.register_part("engine-1", "engine", compatible_models=("bike",))
    game.report_destroyed("battle-1", "bike-1", "killer-1")
    assert game.can_drive("bike-1") is False
    with pytest.raises(GarageError) as exc:
        game.install_part("bike-1", "engine", "engine-1")
    assert exc.value.code == "vehicle_destroyed"


def test_battle_kill_count_is_per_vehicle():
    game = _game()
    game.register_vehicle("bike-2", "bike", 1, "owner")
    game.report_destroyed("battle-2", "bike-1", "killer-1")
    game.report_destroyed("battle-2", "bike-1", "killer-1")
    game.report_destroyed("battle-2", "bike-2", "killer-2")
    assert game.kill_count("battle-2") == 2


def test_transfer_keeps_installed_parts():
    game = _game()
    game.register_part("engine-1", "engine", compatible_models=("bike",))
    game.install_part("bike-1", "engine", "engine-1")
    game.transfer("bike-1", "main", "garage-b")
    assert game.garage_vehicles("garage-b") == ["bike-1"]
    assert game.combat_snapshot("bike-1")["slots"] == {"engine": "engine-1"}


def test_applying_preset_replaces_not_unions_slots():
    game = _game()
    game.register_part("engine-1", "engine", compatible_models=("bike",),
                       stats={"power": 10})
    game.register_part("engine-2", "engine", compatible_models=("bike",),
                       stats={"power": 20})
    game.register_part("armor-1", "armor", compatible_models=("bike",),
                       stats={"armor": 5})
    game.install_part("bike-1", "engine", "engine-1")
    game.install_part("bike-1", "armor", "armor-1")
    game.save_preset("bike-1", "sport")
    game.remove_part("bike-1", "engine")
    game.install_part("bike-1", "engine", "engine-2")
    game.save_preset("bike-1", "power")
    game.apply_preset("bike-1", "power")
    assert game.combat_snapshot("bike-1")["slots"] == {
        "engine": "engine-2", "armor": "armor-1"}
    game.apply_preset("bike-1", "sport")
    assert game.combat_snapshot("bike-1")["slots"] == {
        "engine": "engine-1", "armor": "armor-1"}
    assert game.part_location("engine-2").startswith("warehouse")


def test_season_reset_keeps_limited_livery():
    game = _game()
    game.apply_livery("bike-1", "limited-skin")
    assert game.combat_snapshot("bike-1")["livery"] == "limited-skin"
    game.season_reset()
    assert game.combat_snapshot("bike-1")["livery"] == "limited-skin"
