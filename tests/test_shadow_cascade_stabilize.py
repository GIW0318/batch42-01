import pytest

import shadow_cascade_stabilize
from shadow_cascade_stabilize import CascadeStabilize, ShadowSetupError


def test_zero_cascade_count_names_config_item():
    game = CascadeStabilize()
    with pytest.raises(ShadowSetupError) as exc:
        game.configure(cascade_count=0)
    assert "cascade_count" in str(exc.value)


def test_zero_shadow_map_size_names_config_item():
    game = CascadeStabilize()
    with pytest.raises(ShadowSetupError) as exc:
        game.configure(shadow_map_size=0)
    assert "shadow_map_size" in str(exc.value)


def test_degenerate_light_direction_names_config_item():
    game = CascadeStabilize()
    with pytest.raises(ShadowSetupError) as exc:
        game.configure(light_dir=(0.0, 0.0, 0.0))
    assert "light_dir" in str(exc.value)


def test_camera_translation_snaps_to_whole_texels():
    game = CascadeStabilize(shadow_map_size=1024)
    texel = game.texel_size(0)
    first = game.stabilize(0, (1.234, 0.0, 5.678))
    second = game.stabilize(0, (1.900, 0.0, 6.111))
    for before, after in zip(first, second):
        steps = (after - before) / texel
        assert steps == pytest.approx(round(steps), abs=1e-6)


def test_camera_rotation_only_does_not_move_light_matrix():
    game = CascadeStabilize()
    before = game.light_matrix(0)
    game.stabilize(0, (5.0, 0.0, 5.0))
    after = game.light_matrix(0)
    assert before == after


def test_split_distances_are_monotonic_and_end_at_far():
    game = CascadeStabilize(cascade_count=4)
    splits = game.split_distances(0.1, 200.0)
    assert len(splits) == 4
    assert splits[0] > 0.1
    for left, right in zip(splits, splits[1:]):
        assert right > left
    assert splits[-1] == pytest.approx(200.0)


def test_split_lambda_changes_split_but_keeps_monotonic():
    left = CascadeStabilize(cascade_count=3)
    left.configure(split_lambda=0.2)
    low = left.split_distances(0.1, 200.0)
    right = CascadeStabilize(cascade_count=3)
    right.configure(split_lambda=0.9)
    high = right.split_distances(0.1, 200.0)
    assert low != high
    assert all(one < two for one, two in zip(low, low[1:]))
    assert all(one < two for one, two in zip(high, high[1:]))


def test_cascade_boundary_blend_is_continuous():
    game = CascadeStabilize()
    boundary = game.split_distances(0.1, 200.0)[0]
    left = game.blend(boundary - 1e-6)
    right = game.blend(boundary + 1e-6)
    assert abs(left - right) <= 1e-4
    assert game.blend(boundary) == pytest.approx(0.5, abs=1e-6)


def test_blend_width_is_world_space_not_resolution_scaled():
    low = CascadeStabilize(shadow_map_size=1024, blend_width=10.0)
    high = CascadeStabilize(shadow_map_size=4096, blend_width=10.0)
    boundary = low.split_distances(0.1, 200.0)[0]
    assert low.blend(boundary + 5.0) == pytest.approx(
        high.blend(boundary + 5.0), abs=1e-6)
    assert low.blend(boundary + 5.0) == pytest.approx(0.75, abs=1e-6)


def test_light_atlas_regions_are_disjoint():
    game = CascadeStabilize(atlas_slots=4)
    first = game.atlas_region(1)
    second = game.atlas_region(2)
    assert first != second
    first_x, first_y, first_w, first_h = first
    second_x, second_y, second_w, second_h = second
    disjoint = (first_x + first_w <= second_x
                or second_x + second_w <= first_x
                or first_y + first_h <= second_y
                or second_y + second_h <= first_y)
    assert disjoint is True


def test_atlas_update_budget_defers_extra_lights():
    game = CascadeStabilize(atlas_slots=4, update_budget=2)
    first = game.update_light(1)
    second = game.update_light(2)
    third = game.update_light(3)
    assert first["status"] == "updated"
    assert second["status"] == "updated"
    assert third["status"] == "deferred"
    assert game.budget_report()["deferred_lights"] == 1


def test_contact_shadow_exists_near_feet_only():
    game = CascadeStabilize()
    assert game.contact_shadow(0.2) > 0.0
    assert game.contact_shadow(10.0) == 0.0


def test_transparent_surface_does_not_cast_full_black_shadow():
    game = CascadeStabilize()
    assert game.shadow_strength(0.25) == pytest.approx(0.25, abs=1e-6)
    assert game.shadow_strength(1.0) == pytest.approx(1.0, abs=1e-6)


def test_light_direction_change_updates_matrix():
    game = CascadeStabilize()
    before = game.light_matrix(0)
    game.configure(light_dir=(-0.5, -1.0, -0.2))
    after = game.light_matrix(0)
    assert before != after


def test_each_cascade_has_its_own_light_matrix():
    game = CascadeStabilize(cascade_count=3)
    matrices = [game.light_matrix(index) for index in range(3)]
    assert matrices[0] != matrices[1]
    assert matrices[1] != matrices[2]
