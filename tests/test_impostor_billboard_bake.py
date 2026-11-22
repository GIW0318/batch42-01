import pytest

import impostor_billboard_bake
from impostor_billboard_bake import ImpostorBillboardBake, ImpostorError


def test_direction_frames_cover_yaw_and_pitch():
    game = ImpostorBillboardBake(directions=16, atlas_grid=(4, 4))
    frames = {
        game.direction_frame(0.0, 0.0),
        game.direction_frame(0.0, -60.0),
        game.direction_frame(0.0, 60.0),
        game.direction_frame(90.0, 0.0),
    }
    assert len(frames) == 4


def test_direction_interpolation_does_not_flash_between_angles():
    game = ImpostorBillboardBake(directions=16, atlas_grid=(4, 4))
    first = game.color_at(0.0, 0.0)
    second = game.color_at(5.0, 0.0)
    for left, right in zip(first, second):
        assert abs(left - right) <= 0.1


def test_only_one_model_is_visible_at_each_distance():
    game = ImpostorBillboardBake(switch_distance=50.0)
    assert game.visible_models(49.0) == ["near"]
    assert game.visible_models(51.0) == ["impostor"]
    assert len(game.visible_models(50.0)) == 1


def test_impostor_color_matches_near_model_at_switch():
    game = ImpostorBillboardBake(switch_distance=50.0)
    far = game.color_at(0.0, 0.0)
    near = game.near_color()
    for left, right in zip(far, near):
        assert abs(left - right) <= 0.05


def test_atlas_cells_keep_padding_gutter():
    game = ImpostorBillboardBake(directions=16, atlas_grid=(4, 4), padding=2)
    layout = game.atlas_layout()
    assert layout["padding"] == 2
    cells = layout["cells"]
    for index in range(3):
        left = cells[index]
        right = cells[index + 1]
        if left[1] != right[1]:
            continue
        assert right[0] - (left[0] + left[2]) >= 2


def test_mip_chain_keeps_same_padding():
    game = ImpostorBillboardBake(directions=16, atlas_grid=(4, 4), padding=3)
    layout = game.atlas_layout()
    assert layout["mip_padding"] == 3


def test_instance_color_is_multiplied_once():
    game = ImpostorBillboardBake()
    result = game.instance_color((0.5, 0.5, 0.5), (0.5, 0.5, 0.5))
    assert result == pytest.approx((0.25, 0.25, 0.25))


def test_same_direction_samples_are_deterministic():
    game = ImpostorBillboardBake(directions=16, atlas_grid=(4, 4))
    assert game.color_at(12.0, 10.0) == game.color_at(12.0, 10.0)


def test_looking_up_from_below_still_has_a_frame():
    game = ImpostorBillboardBake(directions=16, atlas_grid=(4, 4))
    frame = game.direction_frame(0.0, -75.0)
    assert isinstance(frame, int)
    assert game.sample(0.0, -75.0)["frame"] == frame


def test_bake_detects_near_plane_clip():
    game = ImpostorBillboardBake()
    with pytest.raises(ImpostorError) as exc:
        game.bake("tree-1", near_plane=999.0)
    assert exc.value.code == "geometry_clipped"
    warned = game.bake("tree-1", near_plane=999.0, raise_on_clip=False)
    assert warned["warning"] == "geometry_clipped"


def test_large_instance_count_stays_in_draw_call_budget():
    game = ImpostorBillboardBake()
    game.set_instance_count(10000)
    report = game.budget_report()
    assert report["draw_calls"] <= report["draw_call_budget"]
    assert report["far_density"] < 1.0


def test_impostor_in_shadow_is_darker_than_lit():
    game = ImpostorBillboardBake()
    lit = sum(game.color_at(0.0, 0.0))
    shadow = sum(game.color_in_shadow(0.0, 0.0))
    assert shadow < lit


def test_fog_darkens_toward_fog_color_without_brightening():
    game = ImpostorBillboardBake()
    clear = game.apply_fog((1.0, 1.0, 1.0), 0.0)
    foggy = game.apply_fog((1.0, 1.0, 1.0), 0.5)
    assert clear == (1.0, 1.0, 1.0)
    assert sum(foggy) <= sum(clear)


def test_nearest_frame_is_used_for_interpolation():
    game = ImpostorBillboardBake(directions=16, atlas_grid=(4, 4))
    first = game.direction_frame(0.0, 0.0)
    second = game.direction_frame(22.5, 0.0)
    middle = game.sample(5.0, 0.0)["frame"]
    assert middle in (first, second)


def test_switching_distance_uses_configured_value():
    game = ImpostorBillboardBake(switch_distance=80.0)
    assert game.visible_models(70.0) == ["near"]
    assert game.visible_models(90.0) == ["impostor"]
