import pytest

import audio_ducking_mixer
from audio_ducking_mixer import AudioDuckingMixer


def test_duck_priority_takes_strictest_not_product():
    game = AudioDuckingMixer()
    game.trigger_duck("boss", "music", -10.0, priority=1)
    game.trigger_duck("voice", "music", -6.0, priority=2)
    assert game.effective_gain("music") == pytest.approx(-6.0)


def test_same_priority_uses_latest_trigger():
    game = AudioDuckingMixer()
    game.trigger_duck("slow", "music", -10.0, priority=1, at=1.0)
    game.trigger_duck("fast", "music", -6.0, priority=1, at=2.0)
    assert game.effective_gain("music") == pytest.approx(-6.0)


def test_ducking_never_goes_below_silence_floor():
    game = AudioDuckingMixer(silence_floor_db=-60.0)
    game.set_bus_gain("music", -30.0)
    game.trigger_duck("boss", "music", -40.0, priority=1)
    assert game.effective_gain("music") == pytest.approx(-60.0)


def test_mute_does_not_change_stored_gain():
    game = AudioDuckingMixer()
    game.set_bus_gain("music", -5.0)
    game.trigger_duck("boss", "music", -10.0, priority=1)
    game.mute("music")
    assert game.is_muted("music") is True
    assert game.bus_gain("music") == pytest.approx(-5.0)
    assert game.audible("music") is False
    game.unmute("music")
    assert game.effective_gain("music") == pytest.approx(-15.0)


def test_solo_makes_only_solo_bus_audible():
    game = AudioDuckingMixer()
    game.solo("voice")
    assert game.audible("voice") is True
    assert game.audible("music") is False
    game.unsolo()
    assert game.audible("music") is True


def test_restart_restores_gains_and_mute_state():
    game = AudioDuckingMixer()
    game.set_bus_gain("music", -4.0)
    game.set_bus_gain("sfx", -2.0)
    game.mute("voice")
    config = game.save_config()

    restored = AudioDuckingMixer()
    ready = restored.load_config(config)
    assert ready["ready"] is True
    assert restored.bus_gain("music") == pytest.approx(-4.0)
    assert restored.bus_gain("sfx") == pytest.approx(-2.0)
    assert restored.is_muted("voice") is True


def test_scene_change_releases_all_ducks():
    game = AudioDuckingMixer()
    game.trigger_duck("boss", "music", -12.0, priority=3)
    game.trigger_duck("cutscene", "sfx", -8.0, priority=2)
    game.release_all()
    assert game.effective_gain("music") == pytest.approx(0.0)
    assert game.effective_gain("sfx") == pytest.approx(0.0)


def test_duck_on_muted_bus_does_not_accumulate_after_unmute():
    game = AudioDuckingMixer()
    game.set_bus_gain("music", -3.0)
    game.mute("music")
    game.trigger_duck("a", "music", -10.0, priority=1)
    game.trigger_duck("b", "music", -10.0, priority=1)
    game.unmute("music")
    assert game.effective_gain("music") == pytest.approx(-13.0)


def test_music_duck_does_not_affect_voice_bus():
    game = AudioDuckingMixer()
    game.set_bus_gain("voice", -1.0)
    game.trigger_duck("boss", "music", -15.0, priority=2)
    assert game.effective_gain("voice") == pytest.approx(-1.0)
    assert game.effective_gain("music") == pytest.approx(-15.0)


def test_automation_and_duck_add_on_one_bus():
    game = AudioDuckingMixer()
    game.set_automation("music", -3.0)
    game.trigger_duck("boss", "music", -6.0, priority=1)
    assert game.effective_gain("music") == pytest.approx(-9.0)


def test_mix_budget_drops_low_priority_sources():
    game = AudioDuckingMixer(budget_sources=32)
    sources = [{"source_id": "s%d" % index, "priority": index}
               for index in range(40)]
    result = game.mix_frame(sources)
    assert result["mixed"] == 32
    assert result["dropped"] == 8
    dropped_ids = {entry["source_id"] for entry in result["dropped_sources"]}
    assert "s0" in dropped_ids and "s7" in dropped_ids
    assert "s39" not in dropped_ids


def test_output_device_change_rebuilds_channel_layout():
    game = AudioDuckingMixer()
    game.rebuild_channels("old-device", "stereo")
    game.rebuild_channels("headset-1", "5.1")
    assert game.channels()["device_id"] == "headset-1"
    assert game.channels()["layout"] == "5.1"


def test_save_during_fade_stores_stable_gain():
    game = AudioDuckingMixer()
    game.set_bus_gain("music", -10.0)
    game.trigger_duck("fade", "music", -20.0, priority=5)
    config = game.save_config()
    assert config["buses"]["music"]["gain"] == pytest.approx(-10.0)


def test_releasing_one_duck_keeps_the_other():
    game = AudioDuckingMixer()
    game.trigger_duck("a", "music", -10.0, priority=1)
    game.trigger_duck("b", "music", -6.0, priority=2)
    game.release_duck("a")
    assert game.effective_gain("music") == pytest.approx(-6.0)
    game.release_duck("b")
    assert game.effective_gain("music") == pytest.approx(0.0)


def test_duck_takes_effect_without_waiting_for_next_frame():
    game = AudioDuckingMixer()
    game.trigger_duck("boss", "music", -9.0, priority=1)
    assert game.effective_gain("music") == pytest.approx(-9.0)
