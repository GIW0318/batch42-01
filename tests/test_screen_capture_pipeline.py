import pytest

import screen_capture_pipeline
from screen_capture_pipeline import CaptureError, ScreenCapturePipeline


def test_long_recording_keeps_audio_video_drift_bounded():
    game = ScreenCapturePipeline(drift_limit=0.05)
    game.start({"fps": 60})
    for index in range(3600):
        game.add_video_frame(index * 1000, duration=1000)
        game.add_audio_packet(index * 1000 + 5, duration=1000)
    assert abs(game.metadata()["av_drift"]) <= 0.05


def test_non_monotonic_timestamp_is_rejected():
    game = ScreenCapturePipeline()
    game.start()
    game.add_video_frame(1000)
    with pytest.raises(CaptureError) as exc:
        game.add_video_frame(500)
    assert exc.value.code == "non_monotonic"


def test_high_frame_rate_frames_are_not_dropped_to_sixty():
    game = ScreenCapturePipeline()
    game.start({"fps": 120})
    for index in range(120):
        game.add_video_frame(index * 8, duration=8)
    metadata = game.metadata()
    assert metadata["video_frames"] == 120
    assert metadata["fps"] == 120


def test_rotation_change_starts_new_track_and_closes_old():
    game = ScreenCapturePipeline()
    game.start()
    game.add_video_frame(0, rotation=0)
    game.add_video_frame(1000, rotation=90)
    tracks = game.metadata()["tracks"]
    video_tracks = [track for track in tracks if track["kind"] == "video"]
    assert len(video_tracks) == 2
    assert video_tracks[0]["closed"] is True
    assert video_tracks[1]["rotation"] == 90


def test_resolution_change_starts_new_track():
    game = ScreenCapturePipeline()
    game.start()
    game.add_video_frame(0, width=1920, height=1080)
    game.add_video_frame(1000, width=1080, height=1920)
    video_tracks = [track for track in game.metadata()["tracks"]
                    if track["kind"] == "video"]
    assert len(video_tracks) == 2
    assert video_tracks[1]["width"] == 1080
    assert video_tracks[1]["height"] == 1920


def test_pause_resume_keeps_timeline_continuous():
    game = ScreenCapturePipeline()
    game.start()
    game.add_video_frame(0)
    game.pause()
    game.add_video_frame(5000)
    game.resume()
    game.add_video_frame(6000)
    metadata = game.metadata()
    assert metadata["timeline_continuous"] is True
    assert metadata["video_frames"] == 2


def test_first_frame_after_resume_is_counted():
    game = ScreenCapturePipeline()
    game.start()
    game.pause()
    game.resume()
    game.add_video_frame(0)
    assert game.metadata()["video_frames"] == 1


def test_dropped_frames_are_reported_exactly():
    game = ScreenCapturePipeline()
    game.start()
    game.add_video_frame(0)
    game.add_dropped_frame(5)
    game.add_dropped_frame(2)
    assert game.metadata()["dropped_frames"] == 7


def test_encoder_failure_marks_file_damaged_on_finalize():
    game = ScreenCapturePipeline()
    game.start()
    game.add_video_frame(0)
    game.fail_encoder()
    with pytest.raises(CaptureError) as exc:
        game.finalize()
    assert exc.value.code == "encoder_failed"
    assert game.metadata()["status"] == "damaged"


def test_recover_parts_produces_playable_file():
    game = ScreenCapturePipeline()
    game.start()
    for index in range(10):
        game.add_video_frame(index * 1000)
        game.add_audio_packet(index * 1000 + 2)
    game.fail_encoder()
    recovered = game.recover_parts()
    assert recovered["playable"] is True
    assert recovered["duration"] > 0
    assert recovered["video_frames"] == 10


def test_source_switch_drops_black_frames_from_count():
    game = ScreenCapturePipeline()
    game.start()
    game.add_video_frame(0)
    game.set_source("fullscreen")
    game.add_video_frame(1000)
    metadata = game.metadata()
    assert metadata["source"] == "fullscreen"
    assert metadata["video_frames"] == 2
    assert metadata.get("black_frames", 0) == 0


def test_keyframe_interval_is_at_most_two_seconds():
    game = ScreenCapturePipeline()
    game.start({"fps": 60})
    for index in range(240):
        game.add_video_frame(index * 16, duration=16, keyframe=(index % 120 == 0))
    assert game.metadata()["max_keyframe_interval"] <= 2.0


def test_hdr_input_is_tonemapped_into_sdr_metadata():
    game = ScreenCapturePipeline()
    game.start({"hdr": True})
    game.add_video_frame(0)
    color = game.metadata()["color"]
    assert color["hdr_input"] is True
    assert color["transfer"] == "sdr_tonemapped"


def test_cursor_is_recorded_independently_of_monitor():
    game = ScreenCapturePipeline()
    game.start()
    game.set_cursor(True)
    game.add_video_frame(0)
    assert game.metadata()["cursor"] is True


def test_first_audio_packet_defines_sync_offset():
    game = ScreenCapturePipeline(drift_limit=0.05)
    game.start()
    game.add_video_frame(0)
    game.add_audio_packet(3)
    assert abs(game.metadata()["av_offset"]) <= 0.05
