import pytest

import pet_breeding_genetics
from pet_breeding_genetics import (
    BreedingError,
    InbreedingBlocked,
    IncubationQueueFull,
    PetBreedingGenetics,
)


def _pair(game, owner="owner"):
    game.register_pet("pet-a", owner)
    game.register_pet("pet-b", owner)
    game.grant_materials(owner, 20)


def test_breed_deducts_cooldown_from_both_parents_once():
    game = PetBreedingGenetics()
    _pair(game)
    result = game.breed("house-1", "pet-a", "pet-b", materials=2)
    assert len(result["egg_ids"]) == 1
    assert game.breeding_cooldown("pet-a") == 600
    assert game.breeding_cooldown("pet-b") == 600
    assert game.material_balance("owner") == 18


def test_same_pair_cannot_start_in_two_houses():
    game = PetBreedingGenetics()
    _pair(game)
    game.breed("house-1", "pet-a", "pet-b", materials=2)
    with pytest.raises(BreedingError) as exc:
        game.breed("house-2", "pet-a", "pet-b", materials=2)
    assert exc.value.code == "cooldown"
    assert game.material_balance("owner") == 18
    assert len(game.incubating("owner")) == 1


def test_twins_produce_two_eggs_and_charge_materials_once():
    game = PetBreedingGenetics()
    game.register_pet("twin-a", "owner", traits=("twins",))
    game.register_pet("twin-b", "owner")
    game.grant_materials("owner", 10)
    result = game.breed("house-1", "twin-a", "twin-b", materials=2)
    assert len(result["egg_ids"]) == 2
    assert game.material_balance("owner") == 8


def test_parent_on_market_aborts_whole_breed_and_refunds():
    game = PetBreedingGenetics()
    _pair(game)
    game.set_on_market("pet-a", True)
    with pytest.raises(BreedingError) as exc:
        game.breed("house-1", "pet-a", "pet-b", materials=2)
    assert exc.value.code == "parent_unavailable"
    assert game.material_balance("owner") == 20
    assert game.cooldown_remaining("pet-b") == 0
    assert game.incubating("owner") == []


def test_inbreeding_threshold_rejects_without_consuming_state():
    game = PetBreedingGenetics()
    game.register_pet("grand-sire", "owner")
    game.register_pet("grand-dam", "owner")
    game.register_pet("sibling-a", "owner",
                      parents=("grand-sire", "grand-dam"))
    game.register_pet("sibling-b", "owner",
                      parents=("grand-sire", "grand-dam"))
    game.grant_materials("owner", 10)
    with pytest.raises(InbreedingBlocked) as exc:
        game.breed("house-1", "sibling-a", "sibling-b", materials=2)
    assert exc.value.code == "inbreeding_blocked"
    assert game.material_balance("owner") == 10
    assert game.cooldown_remaining("sibling-a") == 0
    assert game.incubating("owner") == []


def test_parent_child_coefficient_is_detected():
    game = PetBreedingGenetics()
    game.register_pet("father", "owner")
    game.register_pet("mother", "owner")
    game.register_pet("child", "owner", parents=("father", "mother"))
    coefficient = game.inbreeding_coefficient("father", "child")
    assert coefficient == pytest.approx(0.25, abs=1e-9)
    with pytest.raises(InbreedingBlocked):
        game.breed("house-1", "father", "child", materials=2)


def test_egg_claim_is_exclusive():
    game = PetBreedingGenetics()
    _pair(game)
    egg_id = game.breed("house-1", "pet-a", "pet-b", materials=2)["egg_ids"][0]
    game.advance(300)
    first = game.claim_egg(egg_id, "owner")
    assert first["status"] == "claimed"
    second = game.claim_egg(egg_id, "owner")
    assert second["status"] == "egg_claimed"
    third = game.claim_egg(egg_id, "other")
    assert third["status"] == "egg_claimed"
    assert len(game.owned_pets("owner")) == 3
    assert game.owned_pets("other") == []


def test_full_incubation_queue_does_not_start_breeding():
    game = PetBreedingGenetics(incubation_capacity=1)
    game.register_pet("a1", "owner")
    game.register_pet("b1", "owner")
    game.register_pet("a2", "owner")
    game.register_pet("b2", "owner")
    game.grant_materials("owner", 20)
    game.breed("house-1", "a1", "b1", materials=2)
    with pytest.raises(IncubationQueueFull) as exc:
        game.breed("house-2", "a2", "b2", materials=2)
    assert exc.value.code == "incubation_full"
    assert game.material_balance("owner") == 18
    assert game.cooldown_remaining("a2") == 0


def test_boost_in_last_seconds_never_extends_incubation():
    game = PetBreedingGenetics()
    _pair(game)
    egg_id = game.breed("house-1", "pet-a", "pet-b", materials=2)["egg_ids"][0]
    game.advance(295)
    assert game.egg_state(egg_id)["remaining"] == 5
    game.boost(egg_id, 60)
    assert game.egg_state(egg_id)["remaining"] == 0
    assert game.claim_egg(egg_id, "owner")["status"] == "claimed"


def test_restart_restores_egg_remaining_time_and_traits():
    game = PetBreedingGenetics(random_seed=11)
    _pair(game)
    egg_id = game.breed("house-1", "pet-a", "pet-b", materials=2)["egg_ids"][0]
    before = game.egg_state(egg_id)
    game.advance(40)
    snapshot = game.snapshot()

    restored = PetBreedingGenetics()
    restored.restore(snapshot)
    after = restored.egg_state(egg_id)
    assert after["remaining"] == 260
    assert after["traits"] == before["traits"]
    restored.advance(260)
    claimed = restored.claim_egg(egg_id, "owner")
    assert claimed["traits"] == before["traits"]


def test_restart_does_not_revive_claimed_egg():
    game = PetBreedingGenetics()
    _pair(game)
    egg_id = game.breed("house-1", "pet-a", "pet-b", materials=2)["egg_ids"][0]
    game.advance(300)
    game.claim_egg(egg_id, "owner")
    snapshot = game.snapshot()

    restored = PetBreedingGenetics()
    restored.restore(snapshot)
    assert restored.egg_state(egg_id)["status"] == "claimed"
    with pytest.raises(BreedingError) as exc:
        restored.boost(egg_id, 30)
    assert exc.value.code == "egg_claimed"
    assert len(restored.owned_pets("owner")) == 3


def test_batch_claim_hatches_every_claimed_egg():
    game = PetBreedingGenetics(incubation_capacity=6)
    for index in range(3):
        game.register_pet("male-%d" % index, "owner")
        game.register_pet("female-%d" % index, "owner")
    game.grant_materials("owner", 20)
    eggs = []
    for index in range(3):
        eggs.extend(game.breed(
            "house-%d" % index, "male-%d" % index, "female-%d" % index,
            materials=2)["egg_ids"])
    game.advance(300)
    result = game.batch_claim(eggs, "owner")
    assert result["claimed"] == eggs
    assert len(game.owned_pets("owner")) == 9


def test_incubating_list_is_scoped_to_owner():
    game = PetBreedingGenetics()
    _pair(game)
    egg_id = game.breed("house-1", "pet-a", "pet-b", materials=2)["egg_ids"][0]
    assert game.incubating("owner") == [egg_id]
    assert game.incubating("other") == []
    new_state = game.egg_state(egg_id)
    assert new_state["owner_id"] == "owner"


def test_client_luck_does_not_change_breed_result():
    left = PetBreedingGenetics(random_seed=23)
    right = PetBreedingGenetics(random_seed=23)
    for game in (left, right):
        game.register_pet("m", "owner", traits=("rare-line",))
        game.register_pet("f", "owner", traits=("rare-line",))
        game.grant_materials("owner", 10)
    left_egg = left.breed("house", "m", "f", materials=2, client_luck=0)["egg_ids"][0]
    right_egg = right.breed("house", "m", "f", materials=2, client_luck=999999)["egg_ids"][0]
    assert left.egg_state(left_egg)["traits"] == right.egg_state(right_egg)["traits"]


def test_cooldown_remaining_uses_server_clock():
    game = PetBreedingGenetics()
    _pair(game)
    game.breed("house-1", "pet-a", "pet-b", materials=2)
    game.advance(100)
    assert game.cooldown_remaining("pet-a") == 500
    game.advance(500)
    assert game.cooldown_remaining("pet-a") == 0
    second = game.breed("house-1", "pet-a", "pet-b", materials=2)
    assert len(second["egg_ids"]) == 1
