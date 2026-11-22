import pytest

import toon_rim_outline as toon


def make():
    outline = toon.ToonOutline(width_px=2.0)
    outline.set_normal_map([[0.0, 0.0, 1.0]])
    return outline


def test_line_width_is_constant_in_screen_pixels_at_any_distance():
    outline = make()
    assert outline.compute_width(1.0) == 2.0
    assert outline.compute_width(20.0) == 2.0


def test_line_width_scales_with_resolution_height():
    outline = make()
    outline.set_resolution(3840, 2160)
    assert outline.effective_width() == pytest.approx(4.0)


def test_ultrawide_resolution_uses_the_height_baseline():
    outline = make()
    outline.set_resolution(3440, 1440)
    assert outline.effective_width() == pytest.approx(2.0 * 1440.0 / 1080.0)


def test_sharp_normal_angle_creates_an_internal_edge():
    outline = make()
    edge = outline.classify_edge((0.0, 0.0, 1.0), (0.0, 1.0, 0.0),
                                 5.0, 5.0)
    assert edge == "internal"


def test_large_depth_difference_creates_a_silhouette_edge():
    outline = make()
    edge = outline.classify_edge((0.0, 0.0, 1.0), (0.0, 0.0, 1.0),
                                 2.0, 20.0)
    assert edge == "silhouette"


def test_smoothed_normals_keep_hard_edge_flags():
    outline = make()
    outline.set_normals([
        {"normal": (0.0, 0.0, 1.0), "hard_edge": True},
        {"normal": (0.6, 0.0, 0.8), "hard_edge": True},
    ])
    smoothed = outline.smooth_normals()
    assert smoothed[0]["hard_edge"] is True
    edge = outline.classify_edge(smoothed[0]["normal"], smoothed[1]["normal"],
                                 5.0, 5.0, hard_edge=True)
    assert edge == "silhouette"


def test_outline_behind_a_wall_is_occluded():
    outline = make()
    assert outline.is_occluded(5.0, 3.0) is True
    assert outline.is_occluded(3.0, 5.0) is False


def test_both_vr_eyes_use_identical_width_and_offset():
    outline = make()
    params = outline.vr_params()
    assert params["left"] == params["right"]
    assert params["left"]["width"] == pytest.approx(2.0)
    assert params["left"]["offset"] == pytest.approx(0.0)


def test_postprocess_cannot_alter_outline_coverage():
    outline = make()
    outline.apply_postprocess([1, 0, 1])
    assert outline.coverage() == [1, 0, 1]


def test_missing_normal_map_raises():
    outline = make()
    with pytest.raises(toon.OutlineSetupError):
        outline.set_normal_map(None)


def test_zero_resolution_raises():
    outline = make()
    with pytest.raises(toon.OutlineSetupError):
        outline.set_resolution(0, 1080)


def test_invalid_line_width_raises():
    with pytest.raises(toon.OutlineSetupError):
        toon.ToonOutline(width_px=0.0)


def test_budget_reduces_quality_but_never_line_width():
    outline = make()
    outline.set_budget(0.5)
    assert outline.quality() < 1.0
    assert outline.effective_width() == pytest.approx(2.0)


def test_outline_pass_is_drawn_between_opaque_and_transparent():
    outline = make()
    assert outline.render_order() == ["opaque", "outline", "transparent"]


def test_shallow_crease_does_not_get_an_internal_outline():
    outline = make()
    edge = outline.classify_edge((0.0, 0.0, 1.0), (0.1, 0.0, 0.99),
                                 5.0, 5.01)
    assert edge is None
