import pytest

import midi_clock_conductor as midi


def make():
    conductor = midi.MidiConductor(tempo_bpm=120.0, ppq=480)
    conductor.add_port("synth")
    conductor.play()
    return conductor


def note(port, pitch, beat, duration=1.0, velocity=100):
    return {"port": port, "note": pitch, "velocity": velocity, "beat": beat,
            "duration_beats": duration}


def test_events_are_sent_in_timestamp_order():
    conductor = make()
    conductor.schedule_note("synth", 64, 100, 2.0, 1.0)
    conductor.schedule_note("synth", 60, 100, 0.0, 1.0)
    conductor.schedule_note("synth", 62, 100, 1.0, 1.0)
    sent = conductor.advance(2000.0)
    assert [event["note"] for event in sent if event["type"] == "note_on"] == [
        60, 62, 64]


def test_same_timestamp_keeps_submission_order():
    conductor = make()
    conductor.schedule_note("synth", 60, 100, 0.0, 1.0)
    conductor.schedule_note("synth", 64, 100, 0.0, 1.0)
    sent = conductor.advance(0.0)
    assert [event["note"] for event in sent if event["type"] == "note_on"] == [
        60, 64]


def test_note_off_is_emitted_after_its_duration():
    conductor = make()
    conductor.schedule_note("synth", 60, 100, 0.0, 1.0)
    sent = conductor.advance(500.0)
    kinds = [(event["type"], event["note"]) for event in sent]
    assert ("note_on", 60) in kinds
    assert ("note_off", 60) in kinds
    assert conductor.active_notes("synth") == []


def test_stop_forces_all_notes_off():
    conductor = make()
    conductor.schedule_note("synth", 60, 100, 0.0, 10.0)
    conductor.advance(0.0)
    assert conductor.active_notes("synth") == [60]
    sent = conductor.stop()
    assert any(event["type"] == "all_notes_off" for event in sent)
    assert conductor.active_notes("synth") == []


def test_pause_freezes_the_clock_and_resume_does_not_jump():
    conductor = make()
    conductor.schedule_note("synth", 60, 100, 0.0, 1.0)
    conductor.advance(0.0)
    conductor.pause()
    frozen = conductor.now_ms()
    assert conductor.advance(5000.0) == []
    assert conductor.now_ms() == frozen
    conductor.play()
    sent = conductor.advance(500.0)
    assert any(event["type"] == "note_off" for event in sent)


def test_tempo_change_only_affects_later_events():
    conductor = make()
    conductor.schedule_note("synth", 67, 100, 8.0, 1.0)
    conductor.set_tempo(240.0, at_ms=1000.0)
    sent = conductor.advance(5000.0)
    on = [event for event in sent if event["type"] == "note_on"][0]
    assert on["at_ms"] == pytest.approx(2500.0)


def test_overlapping_same_pitch_notes_are_counted_separately():
    conductor = make()
    conductor.schedule_note("synth", 60, 100, 0.0, 2.0)
    conductor.schedule_note("synth", 60, 90, 0.0, 2.0)
    conductor.advance(0.0)
    assert conductor.active_notes("synth") == [60, 60]
    conductor.advance(1000.0)
    assert conductor.active_notes("synth") == []


def test_loop_timestamps_keep_increasing():
    conductor = make()
    first = conductor.loop()
    second = conductor.loop()
    assert second["start_ms"] > first["start_ms"]


def test_hotplug_rebinds_by_port_name():
    conductor = make()
    conductor.schedule_note("synth", 60, 100, 0.0, 1.0)
    conductor.advance(0.0)
    conductor.remove_port("synth")
    conductor.add_port("synth")
    conductor.schedule_note("synth", 64, 100, 2.0, 1.0)
    sent = conductor.advance(2000.0)
    assert any(event["note"] == 64 and event["port"] == "synth"
               for event in sent)


def test_scheduling_on_missing_port_raises():
    conductor = make()
    with pytest.raises(midi.MidiDeviceError):
        conductor.schedule_note("ghost", 60, 100, 0.0, 1.0)


def test_two_ports_share_the_same_time_base():
    conductor = midi.MidiConductor()
    conductor.add_port("a")
    conductor.add_port("b")
    conductor.play()
    conductor.schedule_note("a", 60, 100, 1.0, 1.0)
    conductor.schedule_note("b", 64, 100, 0.0, 1.0)
    sent = conductor.advance(1000.0)
    ons = [event for event in sent if event["type"] == "note_on"]
    assert [event["port"] for event in ons] == ["b", "a"]


def test_external_clock_drives_the_transport():
    conductor = make()
    conductor.set_external_clock(True, jitter_ms=5.0)
    conductor.external_tick(500.0)
    assert conductor.now_ms() == pytest.approx(500.0)
    conductor.advance(500.0)
    assert conductor.now_ms() == pytest.approx(500.0)


def test_close_sends_all_notes_off():
    conductor = make()
    conductor.schedule_note("synth", 60, 100, 0.0, 10.0)
    conductor.advance(0.0)
    sent = conductor.close()
    assert any(event["type"] == "all_notes_off" for event in sent)
    assert conductor.active_notes("synth") == []


def test_quantize_keeps_weak_pickup_near_its_own_beat():
    conductor = make()
    assert conductor.quantize(20, 480) == 0
    assert conductor.quantize(470, 480) == 480


def test_advance_flushes_only_due_events():
    conductor = make()
    conductor.schedule_note("synth", 60, 100, 0.0, 1.0)
    conductor.schedule_note("synth", 64, 100, 4.0, 1.0)
    first = conductor.advance(100.0)
    assert [event["note"] for event in first if event["type"] == "note_on"] == [
        60]
    second = conductor.advance(2000.0)
    assert [event["note"] for event in second if event["type"] == "note_on"] == [
        64]
