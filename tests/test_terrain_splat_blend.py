import math

import pytest

import terrain_splat_blend
from terrain_splat_blend import TerrainSplatBlend


def test_four_layer_weights_are_normalized_to_one():
    game = TerrainSplatBlend()
    weights = game.normalize_weights([0.4, 0.3, 0.2, 0.1, 0.5])
    assert len(weights) == 4
    assert sum(weights) == pytest.approx(1.0, abs=1e-9)
    assert weights[0] == pytest.approx(0.6, abs=1e-9)


def test_all_zero_weights_fall_back_to_default_layer():
    game = TerrainSplatBlend(default_layer=3)
    assert game.normalize_weights([0.0, 0.0, 0.0, 0.0]) == [0.0, 0.0, 0.0, 1.0]


def test_negative_and_nan_weights_are_sanitized():
    game = TerrainSplatBlend()
    weights = game.normalize_weights([-0.2, float("nan"), 1.2, 0.0])
    assert all(math.isfinite(weight) for weight in weights)
    assert all(0.0 <= weight <= 1.0 for weight in weights)
    assert sum(weights) == pytest.approx(1.0, abs=1e-9)


def test_out_of_range_layer_falls_back_to_default_layer():
    game = TerrainSplatBlend(layer_count=16, default_layer=5)
    assert game.sample_layer(16) == 5
    assert game.sample_layer(-1) == 5
    assert game.sample_layer(15) == 15


def test_uv_is_clamped_to_nearest_valid_texel():
    game = TerrainSplatBlend()
    assert game.clamp_uv(-0.2, 1.3) == (0.0, 1.0)
    assert game.clamp_uv(0.5, 0.5) == (0.5, 0.5)


def test_lod_change_keeps_color_continuous_at_same_position():
    game = TerrainSplatBlend()
    game.set_lod(10)
    near = game.blend_at(3.5, 2.0)
    game.set_lod(200)
    far = game.blend_at(3.5, 2.0)
    assert near["color"][0] == pytest.approx(far["color"][0], abs=0.02)
    assert near["color"][1] == pytest.approx(far["color"][1], abs=0.02)
    assert near["color"][2] == pytest.approx(far["color"][2], abs=0.02)


def test_matching_seam_reports_continuous():
    game = TerrainSplatBlend()
    block = {"weights": [0.6, 0.4, 0.0, 0.0],
             "normal": (0.0, 0.0, 1.0), "height": 5.0}
    report = game.seam_check(block, dict(block))
    assert report["continuous"] is True
    assert report["weight_delta"] == pytest.approx(0.0, abs=1e-9)
    assert report["normal_delta"] == pytest.approx(0.0, abs=1e-9)
    assert report["height_delta"] == pytest.approx(0.0, abs=1e-9)


def test_mismatched_seam_is_flagged_with_weight_delta():
    game = TerrainSplatBlend()
    left = {"weights": [0.6, 0.4, 0.0, 0.0],
            "normal": (0.0, 0.0, 1.0), "height": 5.0}
    right = {"weights": [0.2, 0.8, 0.0, 0.0],
             "normal": (0.0, 0.0, 1.0), "height": 5.0}
    report = game.seam_check(left, right)
    assert report["continuous"] is False
    assert report["weight_delta"] == pytest.approx(0.4, abs=1e-9)


def test_far_distance_degrades_sampling_within_budget():
    game = TerrainSplatBlend(samples_near=12, samples_far=3)
    near = game.budget_report(5)
    far = game.budget_report(500)
    assert near["samples_per_pixel"] <= 12
    assert near["degraded"] is False
    assert far["samples_per_pixel"] < near["samples_per_pixel"]
    assert far["degraded"] is True


def test_eight_bit_quantization_keeps_sum_and_error_bounded():
    game = TerrainSplatBlend()
    weights = game.quantize_weights([0.33, 0.33, 0.34, 0.0], bits=8)
    assert sum(weights) == pytest.approx(1.0, abs=1.0 / 255)
    assert all(0.0 <= weight <= 1.0 for weight in weights)
    for weight in weights:
        assert abs(weight - round(weight * 255) / 255) <= 1.0 / 255


def test_hole_sampling_is_clamped_and_collision_follows_hole():
    game = TerrainSplatBlend()
    game.deform_hole(10.0, 2.0)
    assert game.collision_height(10.0) is None
    assert game.collision_height(20.0) is not None
    result = game.blend_at(10.0, 0.0)
    assert sum(result["weights"]) == pytest.approx(1.0, abs=1e-9)


def test_streamed_block_uses_default_color_until_loaded():
    game = TerrainSplatBlend(default_color=(0.5, 0.5, 0.5))
    game.set_streamed(False)
    loading = game.blend_at(0.0, 0.0)
    assert loading["status"] == "streaming"
    assert loading["color"] == (0.5, 0.5, 0.5)
    game.set_streamed(True)
    assert game.blend_at(0.0, 0.0)["status"] == "ready"


def test_height_blend_alpha_is_bounded():
    game = TerrainSplatBlend(height_blend=True)
    result = game.blend_at(1.0, 1.0)
    assert 0.0 <= result["height_alpha"] <= 1.0
    assert sum(result["weights"]) == pytest.approx(1.0, abs=1e-9)


def test_snowline_depends_on_world_height_not_slope_direction():
    game = TerrainSplatBlend()
    north = game.snow_coverage(10.0, 10.0, (0.0, 0.0, 1.0), 100.0)
    south = game.snow_coverage(10.0, -10.0, (0.0, 0.7, 0.7), 100.0)
    assert north == pytest.approx(south, abs=1e-9)


def test_decal_anchor_is_lod_independent():
    game = TerrainSplatBlend()
    near = game.decal_anchor(3.0, 4.0, 0)
    far = game.decal_anchor(3.0, 4.0, 2)
    assert near == pytest.approx(far, abs=1e-9)
