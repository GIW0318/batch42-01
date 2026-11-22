import pytest

import planar_reflection_clip as pr


def make():
    reflection = pr.PlanarReflection(width=1920, height=1080)
    reflection.add_plane("water", (0.0, 0.0, 0.0), (0.0, 1.0, 0.0))
    return reflection


def test_camera_below_the_plane_disables_reflection():
    reflection = make()
    result = reflection.render("water", (0.0, -1.0, 0.0),
                               [{"id": "sky", "y": 5.0, "layer": 1,
                                 "type": "static"}])
    assert result["enabled"] is False


def test_objects_below_the_plane_are_clipped_out():
    reflection = make()
    result = reflection.render("water", (0.0, 5.0, 0.0), [
        {"id": "boat", "y": 2.0, "layer": 1, "type": "static"},
        {"id": "wreck", "y": -3.0, "layer": 1, "type": "static"},
    ])
    assert result["objects"] == ["boat"]


def test_reflection_matrix_mirrors_points_across_the_plane():
    reflection = make()
    assert reflection.reflect_point("water", (0.0, 3.0, 2.0)) == (
        0.0, -3.0, 2.0)


def test_each_plane_owns_its_own_render_target():
    reflection = make()
    reflection.add_plane("mirror", (10.0, 0.0, 0.0), (1.0, 0.0, 0.0))
    reflection.render("water", (0.0, 5.0, 0.0), [])
    reflection.render("mirror", (15.0, 0.0, 0.0), [])
    targets = reflection.targets()
    assert targets["water"] != targets["mirror"]


def test_window_resize_rebuilds_all_targets():
    reflection = make()
    reflection.render("water", (0.0, 5.0, 0.0), [])
    before = reflection.targets()["water"]
    reflection.set_resolution(2560, 1440)
    after = reflection.targets()["water"]
    assert after != before
    assert reflection.width == 2560


def test_particles_can_be_toggled_per_reflection_pass():
    reflection = make()
    objects = [
        {"id": "flag", "y": 2.0, "layer": 1, "type": "static"},
        {"id": "smoke", "y": 2.0, "layer": 1, "type": "particle"},
    ]
    reflection.set_particles(False)
    assert reflection.render("water", (0.0, 5.0, 0.0), objects)["objects"] == [
        "flag"]
    reflection.set_particles(True)
    assert set(reflection.render("water", (0.0, 5.0, 0.0),
                                 objects)["objects"]) == {"flag", "smoke"}


def test_layer_mask_excludes_non_reflective_objects():
    reflection = make()
    reflection.set_layers(1)
    result = reflection.render("water", (0.0, 5.0, 0.0), [
        {"id": "wall", "y": 2.0, "layer": 1, "type": "static"},
        {"id": "ui", "y": 2.0, "layer": 2, "type": "static"},
    ])
    assert result["objects"] == ["wall"]


def test_recursive_reflection_is_limited_to_one_bounce():
    reflection = make()
    result = reflection.render("water", (0.0, 5.0, 0.0), [
        {"id": "mirror", "y": 2.0, "layer": 1, "type": "mirror"},
    ])
    assert result["recursion"] == 1


def test_invalid_plane_normal_and_zero_target_size_raise():
    reflection = make()
    with pytest.raises(pr.ReflectionSetupError):
        reflection.add_plane("bad", (0.0, 0.0, 0.0), (0.0, 0.0, 0.0))
    with pytest.raises(pr.ReflectionSetupError):
        reflection.set_resolution(0, 1080)


def test_budget_lowers_resolution_but_keeps_the_matrix():
    reflection = make()
    point = reflection.reflect_point("water", (1.0, 2.0, 3.0))
    reflection.set_budget(0.01)
    result = reflection.render("water", (0.0, 5.0, 0.0), [])
    assert result["downsampled"] is True
    assert reflection.reflect_point("water", (1.0, 2.0, 3.0)) == point


def test_near_clip_plane_does_not_cut_the_feet():
    reflection = make()
    result = reflection.render("water", (0.0, 5.0, 0.0), [
        {"id": "feet", "y": 0.0, "layer": 1, "type": "dynamic"},
    ])
    assert result["objects"] == ["feet"]


def test_wave_normal_offsets_reflection_coordinates():
    reflection = make()
    flat = reflection.uv_offset("water", (0.5, 0.5))
    reflection.set_wave_normal("water", (0.3, 0.0, 0.0))
    waved = reflection.uv_offset("water", (0.5, 0.5))
    assert flat != waved


def test_scene_switch_clears_old_reflection_targets():
    reflection = make()
    reflection.render("water", (0.0, 5.0, 0.0), [])
    reflection.reset_scene()
    assert reflection.targets() == {}


def test_reflection_texture_matches_screen_resolution_after_resize():
    reflection = make()
    reflection.set_resolution(3840, 2160)
    result = reflection.render("water", (0.0, 5.0, 0.0), [])
    assert result["width"] == 3840
    assert result["height"] == 2160
    assert result["width"] / result["height"] == pytest.approx(3840.0 / 2160.0)


def test_static_and_dynamic_objects_are_culled_separately():
    reflection = make()
    result = reflection.render("water", (0.0, 5.0, 0.0), [
        {"id": "rock", "y": 2.0, "layer": 1, "type": "static"},
        {"id": "npc", "y": 2.0, "layer": 1, "type": "dynamic"},
        {"id": "fish", "y": -2.0, "layer": 1, "type": "dynamic"},
    ])
    assert result["static_culled"] == 0
    assert result["dynamic_culled"] == 1
