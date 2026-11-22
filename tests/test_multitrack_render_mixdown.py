import pytest

import multitrack_render_mixdown
from multitrack_render_mixdown import MultitrackRenderMixdown


def test_stems_sum_to_master_sample_by_sample():
    game = MultitrackRenderMixdown()
    game.add_track("drums", samples=[0.1, 0.2, 0.3])
    game.add_track("bass", samples=[0.05, 0.0, -0.1])
    master = game.render_master()["samples"]
    drums = game.render_stem("drums")["samples"]
    bass = game.render_stem("bass")["samples"]
    for index, value in enumerate(master):
        assert drums[index] + bass[index] == pytest.approx(value, abs=1e-9)


def test_muted_track_is_not_rendered_into_its_stem():
    game = MultitrackRenderMixdown()
    game.add_track("drums", samples=[0.1, 0.2])
    game.set_mute("drums", True)
    stem = game.render_stem("drums")
    assert stem["rendered"] is False
    assert stem["samples"] == []
    assert game.render_master()["samples"] == [0.0, 0.0]


def test_solo_state_is_frozen_when_export_begins():
    game = MultitrackRenderMixdown()
    game.add_track("drums", samples=[1.0])
    game.add_track("bass", samples=[1.0])
    game.set_solo("drums", True)
    tx = game.begin_export()
    game.set_solo("bass", True)
    result = game.commit_export(tx)
    assert result["stems"] == ["drums"]


def test_plugin_delay_is_compensated_sample_exact():
    game = MultitrackRenderMixdown()
    game.add_track("vox", samples=[1.0, 0.0, 0.0, 0.0])
    game.set_plugin_delay("vox", 2)
    master = game.render_master()["samples"]
    assert master[0] == pytest.approx(1.0)
    assert master[2] == pytest.approx(0.0)
    stem = game.render_stem("vox")["samples"]
    assert stem[0] == pytest.approx(1.0)


def test_clip_crossfade_has_no_discontinuity():
    game = MultitrackRenderMixdown()
    game.add_track("pad")
    game.add_clip("pad", 0, 100, fade_out=10)
    game.add_clip("pad", 100, 100, fade_in=10)
    samples = game.render_master()["samples"]
    assert len(samples) == 200
    biggest_step = max(abs(right - left)
                       for left, right in zip(samples, samples[1:]))
    assert biggest_step <= 0.05


def test_export_converts_sample_rate_and_keeps_duration():
    game = MultitrackRenderMixdown(sample_rate=48000)
    game.add_track("tone", samples=[0.5] * 48000)
    result = game.export("out.wav", sample_rate=44100)
    assert result["status"] == "exported"
    assert result["samples"] == 44100
    assert result["duration"] == pytest.approx(1.0, abs=1e-6)


def test_disk_failure_rolls_back_export_state():
    game = MultitrackRenderMixdown()
    game.add_track("drums", samples=[0.1])
    result = game.export("out.wav", fail_disk=True)
    assert result["status"] == "failed"
    assert result["temp_files"] == []
    assert game.export_status()["exported"] is False


def test_clipping_is_reported_instead_of_silent():
    game = MultitrackRenderMixdown()
    game.add_track("hot", samples=[1.5, -1.2])
    result = game.export("out.wav")
    assert result["clipped"] is True
    assert result["peak"] > 1.0


def test_export_status_only_set_after_success():
    game = MultitrackRenderMixdown()
    game.add_track("drums", samples=[0.1])
    assert game.export_status()["exported"] is False
    game.export("out.wav")
    assert game.export_status()["exported"] is True


def test_recover_export_skips_already_rendered_stems():
    game = MultitrackRenderMixdown()
    game.add_track("drums", samples=[0.1])
    game.add_track("bass", samples=[0.2])
    tx = game.begin_export()
    game.complete_stem(tx, "drums")
    game.complete_stem(tx, "bass")
    recovered = game.recover_export(tx)
    assert recovered["completed"] == ["drums", "bass"]
    assert "drums" not in recovered["pending"]
    assert "bass" not in recovered["pending"]


def test_limiter_does_not_truncate_tail():
    game = MultitrackRenderMixdown()
    game.set_limiter(True)
    game.add_track("drums", samples=[0.5] * 100)
    result = game.export("out.wav")
    assert result["samples"] == 100
    assert result["duration"] == pytest.approx(100 / 48000.0)


def test_same_named_tracks_export_to_unique_files():
    game = MultitrackRenderMixdown()
    game.add_track("track-1", samples=[0.1], name="Drums")
    game.add_track("track-2", samples=[0.2], name="Drums")
    result = game.export_stems("stems")
    paths = [entry["path"] for entry in result["files"]]
    assert len(paths) == len(set(paths)) == 2


def test_gain_change_mid_export_uses_begin_snapshot():
    game = MultitrackRenderMixdown()
    game.add_track("a", samples=[0.1, 0.2])
    tx = game.begin_export()
    game.set_track_gain("a", -12.0)
    result = game.commit_export(tx)
    assert result["rendered"]["a"] == pytest.approx([0.1, 0.2])


def test_automation_points_scale_with_export_sample_rate():
    game = MultitrackRenderMixdown(sample_rate=48000)
    game.add_track("a", samples=[0.0] * 48000)
    game.set_automation("a", [(0, -60.0), (48000, 0.0)])
    result = game.export("out.wav", sample_rate=44100)
    assert result["automation_end_sample"] == 44100


def test_peak_reports_master_maximum():
    game = MultitrackRenderMixdown()
    game.add_track("a", samples=[0.2, -0.5])
    assert game.peak() == pytest.approx(0.5)
