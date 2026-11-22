import pytest

import clip_export_range as clip


def make(**kwargs):
    defaults = {
        "duration_ms": 10000,
        "fps": 30,
        "keyframes": [0, 60, 120],
    }
    defaults.update(kwargs)
    return clip.ClipRange(**defaults)


def test_cut_points_are_frame_numbers_not_seconds():
    source = make()
    source.set_range(90, 120)
    assert source.frame_at_ms(3000) == 90
    meta = source.export()
    assert meta["first_frame"] == 90


def test_in_point_after_out_point_raises():
    source = make()
    with pytest.raises(clip.ClipRangeError):
        source.set_range(120, 90)


def test_out_point_beyond_duration_raises():
    source = make()
    with pytest.raises(clip.ClipRangeError):
        source.set_range(0, 99999)


def test_invalid_frame_rate_raises():
    with pytest.raises(clip.ClipRangeError):
        make(fps=0)


def test_decode_starts_at_the_keyframe_before_the_in_point():
    source = make()
    source.set_range(75, 100)
    meta = source.export()
    assert meta["keyframe"] == 60


def test_extra_decoded_frames_are_trimmed():
    source = make()
    source.set_range(30, 60)
    meta = source.export()
    assert meta["frame_count"] == 30


def test_audio_position_is_compensated_for_encoder_priming():
    source = make()
    source.set_range(90, 120)
    meta = source.export(audio_priming_samples=1024)
    assert meta["audio_start_sample"] == 144000 - 1024


def test_rotation_metadata_is_preserved():
    source = make(rotation=90)
    source.set_range(0, 30)
    meta = source.export()
    assert meta["rotation"] == 90


def test_vfr_duration_uses_presentation_timestamps():
    source = make(duration_ms=10000, vfr=True)
    source.set_range(0, 30)
    meta = source.export()
    assert meta["duration_ms"] == 10000


def test_exporting_twice_produces_identical_bytes():
    source = make()
    source.set_range(0, 30)
    first = source.export_bytes()
    second = source.export_bytes()
    assert first == second


def test_disk_full_leaves_a_resumable_partial_export():
    source = make()
    source.set_range(0, 300)
    full = source.export_bytes()
    with pytest.raises(clip.ClipRangeError):
        source.export(disk_limit_bytes=len(full) // 2)
    assert source.resume() == full


def test_subtitles_are_cut_on_whole_cues_only():
    source = make()
    source.set_range(90, 120)
    remaining = source.subtitles([
        {"start_ms": 2900, "end_ms": 3100, "text": "half"},
        {"start_ms": 3200, "end_ms": 3400, "text": "whole"},
    ])
    assert [cue["text"] for cue in remaining] == ["whole"]


def test_audio_is_resampled_to_the_video_sample_rate():
    source = make(sample_rate=44100)
    source.set_range(0, 30)
    meta = source.export()
    assert meta["audio_rate"] == 48000


def test_export_starts_exactly_on_the_in_frame():
    source = make()
    source.set_range(45, 75)
    meta = source.export()
    assert meta["first_frame"] == 45
    assert meta["frame_count"] == 30


def test_decode_only_walks_frames_after_the_chosen_keyframe():
    source = make(keyframes=[0, 300, 600])
    source.set_range(350, 380)
    source.export()
    assert source.decoded_frames() <= 81
