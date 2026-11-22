import math

import pytest

import pitch_track_tuner as tuner


def tone(freq, sample_rate=44100, seconds=0.2, amplitude=0.5):
    return [amplitude * math.sin(2.0 * math.pi * freq * index / sample_rate)
            for index in range(int(sample_rate * seconds))]


def noise(count=4000, seed=1):
    values = []
    state = seed
    for _ in range(count):
        state = (1103515245 * state + 12345) % 2147483648
        values.append((state / 2147483648.0) * 2.0 - 1.0)
    return values


def make(**kwargs):
    return tuner.PitchTuner(**kwargs)


def test_low_e_is_not_reported_an_octave_high():
    device = make()
    device.push_samples(tone(82.41))
    result = device.analyze()
    assert result["detected"] is True
    assert result["frequency"] == pytest.approx(82.41, abs=1.5)


def test_a4_tone_reports_zero_cents():
    device = make()
    device.push_samples(tone(440.0))
    result = device.analyze()
    assert result["note"] == "A4"
    assert result["cents"] == pytest.approx(0.0, abs=1.0)


def test_a4_calibration_shifts_all_cents():
    device = make(a4=442.0)
    device.push_samples(tone(440.0))
    result = device.analyze()
    expected = 1200.0 * math.log(440.0 / 442.0, 2.0)
    assert result["cents"] == pytest.approx(expected, abs=0.5)


def test_same_tone_matches_across_sample_rates():
    low = make(sample_rate=44100)
    low.push_samples(tone(220.0, sample_rate=44100))
    low_result = low.analyze()
    high = make(sample_rate=48000)
    high.push_samples(tone(220.0, sample_rate=48000))
    high_result = high.analyze()
    assert low_result["cents"] == pytest.approx(high_result["cents"], abs=2.0)


def test_silence_returns_to_undetected_state():
    device = make()
    device.push_samples([0.0] * 4000)
    assert device.analyze()["detected"] is False


def test_noise_is_rejected_by_confidence_threshold():
    device = make()
    device.push_samples(noise())
    assert device.analyze()["detected"] is False


def test_audio_callback_only_copies_until_analysis_runs():
    device = make()
    device.push_samples(tone(440.0))
    assert device.analyzed_frames() == 0
    device.analyze()
    assert device.analyzed_frames() == 1


def test_unsupported_sample_rate_raises():
    device = make()
    with pytest.raises(tuner.TunerDeviceError):
        device.open_device(12345)


def test_window_length_is_derived_from_sample_rate():
    device = make(sample_rate=44100, window_ms=80.0)
    assert device.window_samples() == 3528
    device.open_device(48000)
    assert device.window_samples() == 3840


def test_chord_mode_reports_every_string_not_only_the_lowest():
    device = make()
    device.set_mode("chord")
    samples = [a + b for a, b in zip(tone(440.0), tone(660.0))]
    device.push_samples(samples)
    result = device.analyze()
    frequencies = [note["frequency"] for note in result["notes"]]
    assert any(abs(value - 440.0) < 6.0 for value in frequencies)
    assert any(abs(value - 660.0) < 6.0 for value in frequencies)


def test_reset_clears_the_last_detection():
    device = make()
    device.push_samples(tone(440.0))
    device.analyze()
    device.reset()
    assert device.analyze()["detected"] is False


def test_calibration_applies_to_other_notes_too():
    device = make(a4=880.0)
    device.push_samples(tone(880.0))
    result = device.analyze()
    assert result["cents"] == pytest.approx(0.0, abs=1.0)


def test_short_buffer_is_not_analyzed_as_a_note():
    device = make()
    device.push_samples([0.1, -0.1, 0.1, -0.1])
    assert device.analyze()["detected"] is False


def test_needle_returns_to_zero_after_string_stops():
    device = make()
    device.push_samples(tone(440.0))
    assert device.analyze()["detected"] is True
    device.push_samples([0.0] * 4000)
    assert device.analyze()["detected"] is False


def test_octave_error_is_removed_by_harmonic_consistency():
    device = make()
    samples = [0.3 * math.sin(2.0 * math.pi * 82.41 * i / 44100)
               + 0.2 * math.sin(2.0 * math.pi * 164.82 * i / 44100)
               for i in range(8820)]
    device.push_samples(samples)
    result = device.analyze()
    assert result["frequency"] == pytest.approx(82.41, abs=2.0)
