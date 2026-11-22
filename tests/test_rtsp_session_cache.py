import pytest

import rtsp_session_cache
from rtsp_session_cache import RtspError, RtspSessionCache


def _cache(**kwargs):
    cache = RtspSessionCache(**kwargs)
    session_id = cache.create_session("rtsp://camera1/stream", "conn1")
    return cache, session_id


def test_session_header_is_normalized():
    cache, session_id = _cache()
    assert cache.normalize_session_id(" Session: AbC123 ") == "abc123"
    assert cache.normalize_session_id("ABC123") == "abc123"
    assert session_id


def test_play_before_setup_rejected():
    cache, session_id = _cache()
    with pytest.raises(RtspError) as exc:
        cache.play(session_id)
    assert exc.value.code == "invalid-state"


def test_attach_second_control_connection_rejected():
    cache, session_id = _cache()
    assert cache.attach(session_id, "conn1") is True
    with pytest.raises(RtspError) as exc:
        cache.attach(session_id, "conn2")
    assert exc.value.code == "session-in-use"


def test_channel_conflict_raises():
    cache, session_id = _cache()
    with pytest.raises(RtspError) as exc:
        cache.setup(session_id, "conn1", "video", 2, 2)
    assert exc.value.code == "channel-conflict"


def test_setup_failure_is_atomic_and_retryable():
    cache, session_id = _cache()
    with pytest.raises(RtspError):
        cache.setup(session_id, "conn1", "video", 2, 2)
    assert cache.session_state(session_id) == "init"
    result = cache.setup(session_id, "conn1", "video", 2, 3)
    assert result["state"] == "ready"
    assert cache.session_state(session_id) == "ready"


def test_digest_replay_with_same_nc_rejected():
    cache, session_id = _cache()
    assert cache.authorize_digest(session_id, "nonce-1", 1) is True
    with pytest.raises(RtspError) as exc:
        cache.authorize_digest(session_id, "nonce-1", 1)
    assert exc.value.code == "digest-replay"


def test_digest_higher_nc_accepted():
    cache, session_id = _cache()
    cache.authorize_digest(session_id, "nonce-1", 1)
    assert cache.authorize_digest(session_id, "nonce-1", 2) is True
    with pytest.raises(RtspError):
        cache.authorize_digest(session_id, "nonce-1", 1)


def test_teardown_releases_ports_and_state():
    cache, session_id = _cache()
    cache.setup(session_id, "conn1", "video", 0, 1)
    released = cache.teardown(session_id)
    assert set(released) == {0, 1}
    assert cache.session_state(session_id) == "teardown"
    with pytest.raises(RtspError) as exc:
        cache.play(session_id)
    assert exc.value.code == "invalid-state"


def test_teardown_twice_rejected():
    cache, session_id = _cache()
    cache.setup(session_id, "conn1", "video", 0, 1)
    cache.teardown(session_id)
    with pytest.raises(RtspError) as exc:
        cache.teardown(session_id)
    assert exc.value.code == "invalid-state"


def test_range_beyond_eof_rejected():
    cache, session_id = _cache()
    cache.setup(session_id, "conn1", "video", 0, 1)
    with pytest.raises(RtspError) as exc:
        cache.play(session_id, range_header="npt=0-101", file_size=100)
    assert exc.value.code == "invalid-range"
    played = cache.play(session_id, range_header="npt=0-100", file_size=100)
    assert played["range"] == (0, 100)


def test_rtp_info_requires_sequence_timestamp_pairs():
    cache, session_id = _cache()
    cache.setup(session_id, "conn1", "video", 0, 1)
    with pytest.raises(RtspError) as exc:
        cache.parse_rtp_info(session_id, [{"track": "video", "seq": 10}])
    assert exc.value.code == "rtp-info-mismatch"
    stored = cache.parse_rtp_info(
        session_id, [{"track": "video", "seq": 10, "timestamp": 9000}]
    )
    assert stored["video"]["seq"] == 10
    assert cache.rtp_info(session_id, "video")["timestamp"] == 9000


def test_session_expiry_uses_monotonic_clock():
    cache, session_id = _cache(session_timeout=60)
    cache.advance(61)
    expired = cache.expire()
    assert expired == [session_id]
    assert cache.session_count() == 0


def test_active_session_not_expired():
    cache, session_id = _cache(session_timeout=60)
    cache.setup(session_id, "conn1", "video", 0, 1)
    cache.play(session_id)
    cache.advance(30)
    cache.play(session_id)
    cache.advance(40)
    assert cache.expire() == []
    cache.advance(61)
    assert cache.expire() == [session_id]


def test_session_limit_does_not_evict_playing_session():
    cache, session_id = _cache(max_sessions=1)
    cache.setup(session_id, "conn1", "video", 0, 1)
    cache.play(session_id)
    with pytest.raises(RtspError) as exc:
        cache.create_session("rtsp://camera2/stream", "conn2")
    assert exc.value.code == "session-limit"
    cache.teardown(session_id)
    assert cache.create_session("rtsp://camera2/stream", "conn2")


def test_restart_does_not_reuse_session_ids():
    cache, session_id = _cache()
    snapshot = cache.snapshot()
    restored = RtspSessionCache()
    restored.restore(snapshot)
    new_session = restored.create_session("rtsp://camera2/stream", "conn2")
    assert new_session != session_id
    assert restored.session_count() == 2
