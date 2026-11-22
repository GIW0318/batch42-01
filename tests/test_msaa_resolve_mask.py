import pytest

import msaa_resolve_mask as msaa

RED = (1.0, 0.0, 0.0)
GREEN = (0.0, 1.0, 0.0)
BLUE = (0.0, 0.0, 1.0)


def make(**kwargs):
    return msaa.MsaaResolve(**kwargs)


def test_unsupported_sample_count_raises():
    with pytest.raises(msaa.MsaaSetupError):
        make(samples=3)


def test_zero_resolution_raises():
    with pytest.raises(msaa.MsaaSetupError):
        make(width=0, height=1080)


def test_unsupported_format_raises():
    with pytest.raises(msaa.MsaaSetupError):
        make(format="R8")


def test_uncovered_samples_do_not_bleed_background_into_edges():
    resolver = make()
    color = resolver.resolve_pixel(
        [RED, RED, BLUE, BLUE], [1.0, 1.0, 1.0, 1.0], [1, 1, 0, 0], BLUE)
    assert color == RED


def test_partial_coverage_blends_covered_samples_in_linear_space():
    resolver = make()
    color = resolver.resolve_pixel(
        [RED, GREEN, BLUE, BLUE], [1.0, 1.0, 1.0, 1.0], [0.5, 0.5, 0, 0],
        BLUE)
    assert color[0] == pytest.approx(0.5)
    assert color[1] == pytest.approx(0.5)
    assert color[2] == pytest.approx(0.0)


def test_linear_average_is_not_gamma_average():
    resolver = make()
    color = resolver.resolve_pixel(
        [(100.0, 0.0, 0.0), (4.0, 0.0, 0.0), (0.0, 0.0, 0.0),
         (0.0, 0.0, 0.0)],
        [1.0, 1.0, 1.0, 1.0], [1, 1, 0, 0], (0.0, 0.0, 0.0))
    assert color[0] == pytest.approx(52.0)


def test_hdr_values_are_not_clamped_before_resolve():
    resolver = make()
    color = resolver.resolve_pixel(
        [(10.0, 0.0, 0.0), (10.0, 0.0, 0.0), (0.0, 0.0, 0.0),
         (0.0, 0.0, 0.0)],
        [1.0, 1.0, 1.0, 1.0], [1, 1, 0, 0], (0.0, 0.0, 0.0))
    assert color[0] == pytest.approx(10.0)


def test_depth_resolve_takes_the_conservative_closest_value():
    resolver = make()
    depth = resolver.resolve([RED, RED, RED, RED], [5.0, 2.0, 8.0, 9.0],
                             [1, 1, 1, 1], BLUE)
    assert depth["depth"] == 2.0


def test_stencil_and_coverage_are_resolved_together():
    resolver = make()
    stencil = resolver.resolve_stencil([1, 1, 0, 0], [1, 1, 0, 0])
    assert stencil == 1


def test_resolution_change_rebuilds_the_target():
    resolver = make()
    first = resolver.target()["generation"]
    resolver.set_resolution(2560, 1440)
    second = resolver.target()["generation"]
    assert second > first
    assert resolver.target()["width"] == 2560


def test_sample_count_change_rebuilds_the_target():
    resolver = make()
    first = resolver.target()["generation"]
    resolver.set_samples(8)
    assert resolver.target()["generation"] > first
    assert resolver.target()["samples"] == 8


def test_budget_downsamples_but_keeps_the_coverage_rule():
    resolver = make()
    expected = resolver.resolve_pixel([RED, RED, BLUE, BLUE],
                                      [1.0] * 4, [1, 1, 0, 0], BLUE)
    resolver.set_budget(0.001)
    result = resolver.resolve([RED, RED, BLUE, BLUE], [1.0] * 4,
                              [1, 1, 0, 0], BLUE)
    assert result["downsampled"] is True
    assert result["color"] == expected


def test_vr_eyes_with_mismatched_sample_counts_raise():
    resolver = make()
    resolver.set_eye_samples("left", 4)
    with pytest.raises(msaa.MsaaSetupError):
        resolver.set_eye_samples("right", 2)


def test_postprocess_before_resolve_is_rejected():
    resolver = make()
    with pytest.raises(msaa.MsaaSetupError):
        resolver.set_pipeline_order(["postprocess", "resolve"])


def test_disabling_msaa_clears_the_old_resolve_target():
    resolver = make()
    resolver.set_enabled(False)
    assert resolver.target()["resolved"] is None
