import pytest

import haptic_timeline_mixer as haptic

ONE_CHANNEL = [(0.0, 1.0), (1000.0, 1.0)]


def make(**kwargs):
    mixer = haptic.HapticTimeline(**kwargs)
    mixer.set_device("pad", sample_rate=1000, channel_map=(0,))
    return mixer


def test_duplicate_request_id_does_not_stack_the_same_effect():
    mixer = make()
    mixer.define_effect("rumble", 500, channels=1, priority=1,
                        curve=ONE_CHANNEL)
    mixer.play("req1", "rumble", "pad")
    mixer.play("req1", "rumble", "pad")
    mixer.advance(100)
    samples = mixer.output("pad")
    assert max(sample["channels"][0] for sample in samples) <= 1.0


def test_high_priority_interrupts_and_low_priority_resumes():
    mixer = make()
    mixer.define_effect("step", 1000, channels=1, priority=0,
                        curve=ONE_CHANNEL)
    mixer.define_effect("boom", 200, channels=1, priority=10,
                        curve=ONE_CHANNEL)
    mixer.play("low", "step", "pad", at_ms=0)
    mixer.play("high", "boom", "pad", at_ms=100)
    mixer.advance(1000)
    samples = mixer.output("pad")
    assert any(sample["at_ms"] >= 900 and sample["channels"][0] > 0
               for sample in samples)
    assert any(100 <= sample["at_ms"] <= 300 and sample["channels"][0] > 0
               for sample in samples)


def test_overlapping_effects_are_normalized_instead_of_clipped():
    mixer = make()
    mixer.define_effect("a", 500, channels=1, curve=ONE_CHANNEL)
    mixer.define_effect("b", 500, channels=1, curve=ONE_CHANNEL)
    mixer.play("ra", "a", "pad")
    mixer.play("rb", "b", "pad")
    mixer.advance(200)
    samples = mixer.output("pad")
    assert max(sample["channels"][0] for sample in samples) <= 1.0
    assert max(sample["channels"][0] for sample in samples) > 0.0


def test_motor_stops_when_the_effect_ends():
    mixer = make()
    mixer.define_effect("pulse", 100, channels=1, curve=ONE_CHANNEL)
    mixer.play("req", "pulse", "pad")
    mixer.advance(200)
    samples = mixer.output("pad")
    assert samples[-1]["at_ms"] == pytest.approx(200.0)
    assert samples[-1]["channels"][0] == 0.0


def test_sample_rate_resampling_keeps_the_same_duration():
    slow = haptic.HapticTimeline()
    slow.set_device("slow", sample_rate=100, channel_map=(0,))
    slow.define_effect("long", 500, channels=1, curve=ONE_CHANNEL)
    slow.play("r1", "long", "slow")
    slow.advance(600)
    slow_samples = [sample for sample in slow.output("slow")
                    if sample["channels"][0] > 0]
    fast = haptic.HapticTimeline()
    fast.set_device("fast", sample_rate=1000, channel_map=(0,))
    fast.define_effect("long", 500, channels=1, curve=ONE_CHANNEL)
    fast.play("r1", "long", "fast")
    fast.advance(600)
    fast_samples = [sample for sample in fast.output("fast")
                    if sample["channels"][0] > 0]
    slow_span = slow_samples[-1]["at_ms"] - slow_samples[0]["at_ms"]
    fast_span = fast_samples[-1]["at_ms"] - fast_samples[0]["at_ms"]
    assert slow_span == pytest.approx(fast_span, abs=20.0)


def test_pause_menu_stops_the_vibration():
    mixer = make()
    mixer.define_effect("rumble", 500, channels=1, curve=ONE_CHANNEL)
    mixer.play("req", "rumble", "pad")
    mixer.advance(100)
    mixer.set_paused(True)
    mixer.advance(200)
    assert mixer.output("pad")[-1]["channels"][0] == 0.0


def test_device_channel_map_swaps_left_and_right():
    mixer = haptic.HapticTimeline()
    mixer.set_device("pad", sample_rate=1000, channel_map=(1, 0))
    mixer.define_effect("dual", 200, channels=2,
                        curve=[(0.0, 1.0, 0.0), (200.0, 1.0, 0.0)])
    mixer.play("req", "dual", "pad")
    mixer.advance(50)
    channels = mixer.output("pad")[0]["channels"]
    assert channels[0] == pytest.approx(0.0)
    assert channels[1] == pytest.approx(1.0)


def test_close_sends_a_stop_command_to_every_device():
    mixer = make()
    mixer.define_effect("rumble", 500, channels=1, curve=ONE_CHANNEL)
    mixer.play("req", "rumble", "pad")
    mixer.advance(100)
    stopped = mixer.close()
    assert stopped["pad"] is True
    assert mixer.output("pad")[-1]["channels"][0] == 0.0


def test_intensity_above_maximum_raises():
    mixer = make()
    mixer.define_effect("rumble", 100, channels=1, curve=ONE_CHANNEL)
    with pytest.raises(haptic.HapticConfigError):
        mixer.play("req", "rumble", "pad", intensity=2.0)


def test_negative_effect_duration_raises():
    mixer = make()
    with pytest.raises(haptic.HapticConfigError):
        mixer.define_effect("bad", -1, channels=1, curve=ONE_CHANNEL)


def test_channel_count_mismatch_raises():
    mixer = make()
    mixer.define_effect("stereo", 100, channels=2,
                        curve=[(0.0, 1.0, 1.0), (100.0, 1.0, 1.0)])
    with pytest.raises(haptic.HapticConfigError):
        mixer.play("req", "stereo", "pad")


def test_discontinuous_trigger_curve_is_rejected():
    mixer = make()
    with pytest.raises(haptic.HapticConfigError):
        mixer.define_effect("jumpy", 200, channels=1,
                            curve=[(0.0, 0.0), (100.0, 1.0), (101.0, 0.0)])


def test_heat_derating_lowers_amplitude_but_not_duration():
    mixer = make()
    mixer.define_effect("long", 500, channels=1, curve=ONE_CHANNEL)
    mixer.play("req", "long", "pad")
    mixer.report_heat("pad", 0.5)
    mixer.advance(500)
    samples = mixer.output("pad")
    positive = [sample for sample in samples if sample["channels"][0] > 0]
    assert max(sample["channels"][0] for sample in positive) <= 0.5
    assert positive[-1]["at_ms"] - positive[0]["at_ms"] == pytest.approx(
        480.0, abs=20.0)


def test_frame_budget_lowers_update_rate_but_not_total_duration():
    mixer = make()
    mixer.define_effect("long", 500, channels=1, curve=ONE_CHANNEL)
    mixer.play("req", "long", "pad")
    mixer.frame_budget(10)
    mixer.advance(500)
    samples = mixer.output("pad")
    stamps = [sample["at_ms"] for sample in samples]
    assert min(b - a for a, b in zip(stamps, stamps[1:])) >= 10.0
    positive = [sample for sample in samples if sample["channels"][0] > 0]
    assert positive[-1]["at_ms"] == pytest.approx(490.0, abs=10.0)


def test_removing_a_device_clears_its_queue():
    mixer = make()
    mixer.define_effect("rumble", 500, channels=1, curve=ONE_CHANNEL)
    mixer.play("req", "rumble", "pad")
    mixer.remove_device("pad")
    assert mixer.output("pad") == []
    mixer.set_device("pad2", sample_rate=1000, channel_map=(0,))
    mixer.advance(100)
    assert mixer.output("pad2") == []
