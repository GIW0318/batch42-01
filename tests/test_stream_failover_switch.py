import pytest

import stream_failover_switch as failover


def make(**kwargs):
    switch = failover.FailoverSwitch(**kwargs)
    switch.add_source("primary", "rtmp://primary", priority=0,
                      resolution=(1920, 1080), bitrate=4000)
    switch.add_source("backup1", "rtmp://backup1", priority=1,
                      resolution=(1280, 720), bitrate=3000)
    switch.add_source("backup2", "rtmp://backup2", priority=2,
                      resolution=(1280, 720), bitrate=3000)
    switch.ingest_segment("primary", 1, True, 0)
    return switch


def test_failover_happens_at_the_next_segment_boundary():
    switch = make()
    for _ in range(3):
        switch.report_health("primary", False, 100)
    chosen = switch.trigger_failover(400)
    assert chosen == "backup1"
    segment = switch.ingest_segment("backup1", 2, True, 2000)
    assert segment["source_id"] == "backup1"
    assert switch.current()["switched_at_ms"] == 2000


def test_switch_never_happens_inside_an_already_emitted_segment():
    switch = make()
    switch.report_health("primary", False, 0)
    switch.trigger_failover(100)
    switch.ingest_segment("backup1", 2, True, 1000)
    assert switch.output()[0]["source_id"] == "primary"
    assert switch.output()[1]["source_id"] == "backup1"


def test_new_source_must_start_with_a_keyframe():
    switch = make()
    switch.report_health("primary", False, 0)
    switch.trigger_failover(100)
    with pytest.raises(failover.StreamSourceError):
        switch.ingest_segment("backup1", 2, False, 1000)


def test_timestamps_stay_continuous_across_the_switch():
    switch = make()
    switch.report_health("primary", False, 0)
    switch.trigger_failover(100)
    with pytest.raises(failover.StreamSourceError):
        switch.ingest_segment("backup1", 2, True, 0)


def test_duplicate_switch_request_is_idempotent():
    switch = make()
    switch.report_health("primary", False, 0)
    first = switch.request_switch("req1", "backup1", 100)
    second = switch.request_switch("req1", "backup1", 100)
    assert first is True
    assert second is False
    switch.ingest_segment("backup1", 2, True, 1000)
    assert switch.current()["switch_count"] == 1


def test_switch_back_requires_a_sustained_healthy_window():
    switch = make()
    switch.report_health("primary", False, 0)
    switch.trigger_failover(100)
    switch.ingest_segment("backup1", 2, True, 1000)
    switch.report_health("primary", True, 2000)
    assert switch.request_switch("back", "primary", 3000) is False
    assert switch.request_switch("back2", "primary", 8000) is True


def test_switch_storm_is_capped_within_the_flap_window():
    switch = make()
    switch.report_health("primary", False, 0)
    switch.trigger_failover(100)
    switch.ingest_segment("backup1", 2, True, 1000)
    for index in range(6):
        switch.report_health("primary", True, 1100 + index)
        switch.request_switch("flap%d" % index, "primary", 1200 + index)
        switch.ingest_segment("primary", 3 + index, True, 1300 + index * 100)
    assert switch.current()["switch_count"] <= 1


def test_duplicate_segment_sequence_is_rejected():
    switch = make()
    with pytest.raises(failover.StreamSourceError):
        switch.ingest_segment("primary", 1, True, 100)
    assert len(switch.output()) == 1


def test_empty_source_url_raises():
    switch = failover.FailoverSwitch()
    with pytest.raises(failover.StreamSourceError):
        switch.add_source("bad", "", priority=0)


def test_invalid_token_is_skipped_for_the_next_backup():
    switch = make()
    switch.add_source("backup1", "rtmp://backup1", priority=1, token="expired")
    switch.report_health("primary", False, 0)
    assert switch.trigger_failover(100) == "backup2"


def test_fallback_walks_the_configured_backup_priority_order():
    switch = make()
    switch.add_source("backup1", "rtmp://backup1", priority=1,
                      token="expired")
    switch.add_source("backup2", "rtmp://backup2", priority=2,
                      token="ok")
    switch.report_health("primary", False, 0)
    chosen = switch.trigger_failover(100)
    assert chosen == "backup2"


def test_restart_does_not_resend_an_emitted_segment():
    switch = make()
    saved = switch.snapshot()
    restored = failover.FailoverSwitch()
    restored.restore(saved)
    before = restored.output()
    assert restored.ingest_segment("primary", 1, True, 100) is None
    assert len(restored.output()) == len(before)


def test_audio_and_video_timestamps_are_aligned_after_switch():
    switch = make()
    switch.report_health("primary", False, 0)
    switch.trigger_failover(100)
    segment = switch.ingest_segment("backup1", 2, True, 1000, audio_ms=500)
    assert segment["audio_ms"] == segment["timestamp_ms"]


def test_output_profile_is_rebuilt_for_the_backup_source():
    switch = make()
    switch.report_health("primary", False, 0)
    switch.trigger_failover(100)
    segment = switch.ingest_segment("backup1", 2, True, 1000)
    assert segment["resolution"] == (1280, 720)
    assert segment["bitrate"] == 3000


def test_recording_header_is_written_for_the_new_source():
    switch = make()
    switch.report_health("primary", False, 0)
    switch.trigger_failover(100)
    switch.ingest_segment("backup1", 2, True, 1000)
    header = switch.recording_header()
    assert header["source_id"] == "backup1"
    assert header["header"] is True
