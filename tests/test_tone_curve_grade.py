import pytest

import tone_curve_grade as tg

LUT = {
    (0, 0, 0): (0.0, 0.0, 0.0),
    (1, 0, 0): (1.0, 0.0, 0.0),
    (0, 1, 0): (0.0, 1.0, 0.0),
    (0, 0, 1): (0.0, 0.0, 1.0),
    (1, 1, 1): (1.0, 1.0, 1.0),
}


def make():
    grade = tg.ToneGrade(lut_size=2, exposure_time_ms=300.0)
    grade.set_lut(LUT, size=2)
    return grade


def test_grade_runs_exposure_then_tonemap_then_lut():
    grade = make()
    color = (0.4, 0.3, 0.2)
    expected = grade.sample_lut(grade.tonemap((0.8, 0.6, 0.4)))
    assert grade.grade(color, exposure=2.0) == expected


def test_auto_exposure_moves_gradually_toward_the_target():
    grade = make()
    first = grade.auto_exposure(4.0, 16.0)
    assert 0.25 < first < 1.0
    final = grade.auto_exposure(4.0, 600.0)
    assert final == pytest.approx(0.25)


def test_cutscene_end_resets_to_current_scene_metering():
    grade = make()
    grade.set_scene("indoor", metering=0.2)
    grade.auto_exposure(0.01, 1000.0)
    grade.end_cutscene()
    assert grade.exposure() == pytest.approx(5.0)


def test_lut_sampling_clamps_to_the_atlas_boundary():
    grade = make()
    assert grade.sample_lut((-1.0, -1.0, -1.0)) == (0.0, 0.0, 0.0)
    assert grade.sample_lut((2.0, 2.0, 2.0)) == (1.0, 1.0, 1.0)


def test_lut_sample_on_a_cell_returns_that_cell_color():
    grade = make()
    assert grade.sample_lut((1.0, 0.0, 0.0)) == (1.0, 0.0, 0.0)
    assert grade.sample_lut((0.0, 1.0, 0.0)) == (0.0, 1.0, 0.0)


def test_ui_is_composited_without_tonemapping():
    grade = make()
    assert grade.grade((2.0, 2.0, 2.0), ui=True) == (2.0, 2.0, 2.0)


def test_same_input_produces_identical_output():
    grade = make()
    first = grade.grade((0.3, 0.6, 0.9), exposure=1.7)
    second = grade.grade((0.3, 0.6, 0.9), exposure=1.7)
    assert first == second


def test_hdr_red_stays_red_after_sdr_conversion():
    grade = make()
    red = grade.hdr_to_sdr((4.0, 0.0, 0.0))
    assert red == (1.0, 0.0, 0.0)


def test_highlights_roll_off_instead_of_clipping_to_white():
    grade = make()
    value = grade.tonemap((1000.0, 1000.0, 1000.0))
    assert 0.9 < value[0] < 1.0
    assert grade.tonemap((1.0, 1.0, 1.0))[0] < value[0]


def test_scene_switch_rebuilds_the_lut():
    grade = make()
    other = {(0, 0, 0): (0.5, 0.5, 0.5), (1, 1, 1): (0.5, 0.5, 0.5)}
    grade.set_scene("night", lut=other, metering=0.1)
    assert grade.sample_lut((0.0, 0.0, 0.0)) == (0.5, 0.5, 0.5)


def test_negative_exposure_compensation_does_not_black_out():
    grade = make()
    result = grade.grade((0.5, 0.5, 0.5), exposure=-1.0)
    assert result[0] > 0.0


def test_zero_lut_size_raises():
    grade = make()
    with pytest.raises(tg.GradeConfigError):
        grade.set_lut({}, size=0)


def test_sdr_gamma_conversion_matches_encoding_curve():
    grade = tg.ToneGrade(gamma=2.2)
    encoded = grade.hdr_to_sdr((0.25, 0.25, 0.25))
    assert encoded[0] == pytest.approx(0.25 ** (1.0 / 2.2), abs=0.01)


def test_overlapping_effects_are_tonemapped_once_after_blending():
    grade = make()
    result = grade.composite_layers([(0.0, 0.0, 0.0), (2.0, 2.0, 2.0)])
    assert result == grade.tonemap((1.0, 1.0, 1.0))


def test_negative_exposure_time_is_rejected():
    with pytest.raises(tg.GradeConfigError):
        tg.ToneGrade(exposure_time_ms=-1.0)
