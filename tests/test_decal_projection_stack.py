import math

import pytest

import decal_projection_stack
from decal_projection_stack import DecalProjectionStack


def test_decals_sort_by_layer_then_creation():
    game = DecalProjectionStack()
    first = game.add_decal("wall", (0.0, 0.0, 0.0), (0.0, 0.0, 1.0),
                           layer=1, decal_id="d1")["decal_id"]
    second = game.add_decal("wall", (1.0, 0.0, 0.0), (0.0, 0.0, 1.0),
                            layer=0, decal_id="d2")["decal_id"]
    third = game.add_decal("wall", (2.0, 0.0, 0.0), (0.0, 0.0, 1.0),
                           layer=1, decal_id="d3")["decal_id"]
    assert game.decal_order() == [second, first, third]


def test_sort_order_is_stable_for_same_submission_order():
    left = DecalProjectionStack()
    right = DecalProjectionStack()
    for game in (left, right):
        for index in range(3):
            game.add_decal("wall", (float(index), 0.0, 0.0),
                           (0.0, 0.0, 1.0), layer=0, decal_id="d%d" % index)
    assert left.decal_order() == right.decal_order() == ["d0", "d1", "d2"]


def test_oldest_decal_fades_instead_of_disappearing():
    game = DecalProjectionStack(limit=2)
    game.add_decal("wall", (0.0, 0.0, 0.0), (0.0, 0.0, 1.0), decal_id="old")
    game.add_decal("wall", (1.0, 0.0, 0.0), (0.0, 0.0, 1.0), decal_id="mid")
    game.add_decal("wall", (2.0, 0.0, 0.0), (0.0, 0.0, 1.0), decal_id="new")
    state = game.fade_state("old")
    assert state["state"] in ("fading", "faded")
    assert 0.0 <= state["alpha"] < 1.0
    assert "old" in game.visible_decals()


def test_reducing_limit_fades_excess_decals():
    game = DecalProjectionStack(limit=5)
    for index in range(3):
        game.add_decal("wall", (float(index), 0.0, 0.0), (0.0, 0.0, 1.0),
                       decal_id="d%d" % index)
    game.set_limit(1)
    assert game.fade_state("d0")["alpha"] < 1.0
    assert game.fade_state("d1")["alpha"] < 1.0
    assert game.fade_state("d2")["alpha"] == pytest.approx(1.0)


def test_atlas_sampling_is_clamped_inside_requested_cell():
    game = DecalProjectionStack(atlas_grid=(4, 4), padding=1)
    cell = (1, 1)
    sample = game.sample_atlas(cell, 1.0, 0.5)
    assert sample["cell"] == cell
    assert sample["u"] < 1.0
    assert sample["clamped"] is True
    assert game.atlas_cell_of_sample(cell, 1.0, 0.5) == cell


def test_atlas_mip_sampling_stays_in_cell():
    game = DecalProjectionStack(atlas_grid=(4, 4), padding=1)
    sample = game.sample_atlas((2, 3), 0.5, 0.5, mip=2)
    assert sample["cell"] == (2, 3)
    assert sample["mip"] == 2


def test_clear_scene_removes_every_decal():
    game = DecalProjectionStack()
    game.add_decal("wall", (0.0, 0.0, 0.0), (0.0, 0.0, 1.0), decal_id="d1")
    game.add_decal("floor", (0.0, 0.0, 0.0), (0.0, 1.0, 0.0), decal_id="d2")
    game.clear_scene()
    assert game.decal_order() == []
    assert game.visible_decals() == []


def test_destroyed_host_recycles_its_decals():
    game = DecalProjectionStack()
    game.add_decal("wall", (0.0, 0.0, 0.0), (0.0, 0.0, 1.0), decal_id="wall-1")
    game.add_decal("floor", (0.0, 0.0, 0.0), (0.0, 1.0, 0.0),
                   decal_id="floor-1")
    game.host_destroyed("wall")
    assert game.decal_order() == ["floor-1"]


def test_decal_follows_moving_host():
    game = DecalProjectionStack()
    game.add_decal("wall", (1.0, 2.0, 3.0), (0.0, 0.0, 1.0), decal_id="d1")
    game.update_host("wall", (10.0, 2.0, 3.0), (0.0, 0.0, 1.0))
    transform = game.decal_transform("d1")
    assert transform["position"][0] == pytest.approx(10.0)
    assert transform["position"][1] == pytest.approx(2.0)


def test_decal_normal_stays_unit_length_on_slope():
    game = DecalProjectionStack()
    game.add_decal("slope", (0.0, 0.0, 0.0), (0.0, 0.6, 0.8), decal_id="d1")
    normal = game.decal_transform("d1")["normal"]
    assert math.sqrt(sum(value * value for value in normal)) == pytest.approx(
        1.0, abs=1e-9)


def test_floor_decal_is_visible_from_above_despite_backface():
    game = DecalProjectionStack()
    game.add_decal("floor", (0.0, -1.0, 0.0), (0.0, 1.0, 0.0), decal_id="d1")
    assert "d1" in game.visible_from((0.0, 10.0, 0.0))


def test_duplicate_submission_from_two_systems_is_single_instance():
    game = DecalProjectionStack()
    game.add_decal("wall", (0.0, 0.0, 0.0), (0.0, 0.0, 1.0),
                   system_id="blood", decal_id="d1")
    game.add_decal("wall", (0.0, 0.0, 0.0), (0.0, 0.0, 1.0),
                   system_id="blood", decal_id="d1")
    assert game.decal_order() == ["d1"]


def test_fading_decal_participates_in_depth_test():
    game = DecalProjectionStack(limit=1)
    game.add_decal("wall", (0.0, 0.0, 0.0), (0.0, 0.0, 1.0), decal_id="old")
    game.add_decal("wall", (1.0, 0.0, 0.0), (0.0, 0.0, 1.0), decal_id="new")
    assert "old" in game.visible_decals()
    assert game.depth_tested("old") is True


def test_atlas_sampling_is_repeatable_at_same_coordinates():
    game = DecalProjectionStack(atlas_grid=(4, 4), padding=1)
    first = game.sample_atlas((1, 2), 0.333, 0.777, mip=1)
    second = game.sample_atlas((1, 2), 0.333, 0.777, mip=1)
    assert first == second


def test_host_normal_update_is_applied_to_decal():
    game = DecalProjectionStack()
    game.add_decal("wall", (0.0, 0.0, 0.0), (0.0, 0.0, 1.0), decal_id="d1")
    game.update_host("wall", (0.0, 0.0, 0.0), (0.0, 1.0, 0.0))
    normal = game.decal_transform("d1")["normal"]
    assert normal == pytest.approx((0.0, 1.0, 0.0))
