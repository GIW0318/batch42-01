import pytest

import hls_drm_key_rotation
from hls_drm_key_rotation import (
    DeviceLimitExceeded,
    DrmError,
    HlsDrmKeyRotation,
)


def test_device_limit_rejects_extra_device_without_kicking_old():
    game = HlsDrmKeyRotation(max_devices=4)
    for index in range(4):
        game.bind_license("lic-1", "device-%d" % index)
    with pytest.raises(DeviceLimitExceeded) as exc:
        game.bind_license("lic-1", "device-4")
    assert exc.value.code == "device_limit_exceeded"
    assert game.devices("lic-1") == [
        "device-0", "device-1", "device-2", "device-3"]


def test_unbind_releases_device_slot_immediately():
    game = HlsDrmKeyRotation(max_devices=2)
    game.bind_license("lic-1", "device-0")
    game.bind_license("lic-1", "device-1")
    game.unbind_device("lic-1", "device-0")
    game.bind_license("lic-1", "device-2")
    assert game.devices("lic-1") == ["device-1", "device-2"]


def test_rotation_seam_uses_per_segment_keys():
    game = HlsDrmKeyRotation()
    game.rotate_key("show-1", 0, "key-old")
    game.rotate_key("show-1", 5, "key-new")
    assert game.key_for("show-1", 0) == "key-old"
    assert game.key_for("show-1", 4) == "key-old"
    assert game.key_for("show-1", 5) == "key-new"
    assert game.key_for("show-1", 9) == "key-new"


def test_keys_are_not_mixed_between_programs():
    game = HlsDrmKeyRotation()
    game.rotate_key("episode-1", 0, "key-ep1")
    game.rotate_key("episode-2", 0, "key-ep2")
    assert game.key_for("episode-1", 0) == "key-ep1"
    assert game.key_for("episode-2", 0) == "key-ep2"


def test_rotation_effective_at_server_time():
    game = HlsDrmKeyRotation(server_time=0)
    game.rotate_key("show-1", 0, "key-old")
    game.rotate_key("show-1", 0, "key-new", at=120)
    assert game.key_for("show-1", 0) == "key-old"
    game.advance(120)
    assert game.key_for("show-1", 0) == "key-new"


def test_offline_license_expiry_uses_server_time():
    game = HlsDrmKeyRotation(server_time=100, license_ttl=50)
    game.bind_license("lic-1", "device-0")
    assert game.validate_offline("lic-1") is True
    game.advance(60)
    assert game.validate_offline("lic-1") is False


def test_only_one_writer_can_resume_download():
    game = HlsDrmKeyRotation()
    first = game.acquire_download("task-1", "device-0")
    assert first["status"] == "acquired"
    second = game.acquire_download("task-1", "device-1")
    assert second["status"] == "download_in_progress"
    game.release_download("task-1")
    third = game.acquire_download("task-1", "device-1")
    assert third["status"] == "acquired"


def test_resume_continues_after_last_written_segment():
    game = HlsDrmKeyRotation()
    game.acquire_download("task-1", "device-0")
    for index in range(3):
        game.write_segment("task-1", index)
    assert game.resume_at("task-1") == 3
    game.release_download("task-1")
    game.acquire_download("task-1", "device-0")
    assert game.resume_at("task-1") == 3


def test_unsupported_key_format_raises_explicit_error():
    game = HlsDrmKeyRotation()
    assert game.supports_key_format(2) is True
    assert game.supports_key_format(1) is False
    with pytest.raises(DrmError) as exc:
        game.check_key_format(1)
    assert exc.value.code == "unsupported_key_format"


def test_rate_limit_returns_retry_not_permanent_failure():
    game = HlsDrmKeyRotation(rate_limit=2, rate_window=1.0)
    game.rotate_key("show-1", 0, "key-old")
    assert game.request_key("show-1", 0)["status"] == "ok"
    assert game.request_key("show-1", 0)["status"] == "ok"
    limited = game.request_key("show-1", 0)
    assert limited["status"] == "retry"
    assert limited["retry_after"] > 0
    game.advance(1.0)
    assert game.request_key("show-1", 0)["status"] == "ok"


def test_ad_break_uses_its_own_key():
    game = HlsDrmKeyRotation()
    game.rotate_key("show-1", 0, "content-key")
    game.rotate_ad_key("show-1", 0, "ad-key")
    assert game.ad_key_for("show-1", 0) == "ad-key"
    assert game.ad_key_for("show-1", 0) != game.key_for("show-1", 0)


def test_audit_lists_segment_key_versions():
    game = HlsDrmKeyRotation()
    game.rotate_key("show-1", 0, "key-a")
    game.rotate_key("show-1", 3, "key-b")
    audit = game.audit("show-1")
    assert audit == [
        {"segment": 0, "key_id": "key-a"},
        {"segment": 3, "key_id": "key-b"},
    ]


def test_duplicate_bind_is_idempotent():
    game = HlsDrmKeyRotation()
    game.bind_license("lic-1", "device-0")
    game.bind_license("lic-1", "device-0")
    assert game.devices("lic-1") == ["device-0"]


def test_reconnect_after_rotation_resumes_at_correct_segment():
    game = HlsDrmKeyRotation()
    game.rotate_key("show-1", 0, "key-old")
    game.rotate_key("show-1", 4, "key-new")
    game.acquire_download("task-1", "device-0")
    game.write_segment("task-1", 0)
    game.write_segment("task-1", 1)
    game.release_download("task-1")
    game.acquire_download("task-1", "device-0")
    assert game.resume_at("task-1") == 2
    assert game.key_for("show-1", game.resume_at("task-1")) == "key-old"


def test_rewriting_old_segment_does_not_roll_progress_back():
    game = HlsDrmKeyRotation()
    game.acquire_download("task-1", "device-0")
    game.write_segment("task-1", 0)
    game.write_segment("task-1", 1)
    result = game.write_segment("task-1", 0)
    assert result["status"] == "already_written"
    assert game.resume_at("task-1") == 2
