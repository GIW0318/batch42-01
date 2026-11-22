import pytest

import photo_mode_sandbox as photo


def scene():
    return {
        "position": (1.0, 2.0, 3.0),
        "weather": "dusk",
        "picked": ["ore"],
        "cutscene": "intro",
        "time_ms": 100,
        "progress": 3,
        "scene_name": "village",
    }


def make():
    sandbox = photo.PhotoModeSandbox()
    sandbox.enter(scene())
    return sandbox


def test_enter_freezes_the_local_world():
    sandbox = make()
    before = sandbox.local_state()["time_ms"]
    sandbox.world_tick(500)
    assert sandbox.local_state()["time_ms"] == before
    assert sandbox.local_state()["frozen"] is True


def test_exit_restores_position_weather_pickups_and_cutscene():
    sandbox = make()
    sandbox.set_weather("noon")
    sandbox.world_tick(1000)
    sandbox.exit()
    state = sandbox.local_state()
    assert state["weather"] == "dusk"
    assert tuple(state["position"]) == (1.0, 2.0, 3.0)
    assert state["picked"] == ["ore"]
    assert state["cutscene"] == "intro"


def test_filter_applies_to_scene_layer_not_ui_layer():
    sandbox = make()
    result = sandbox.take_photo(filter_name="sepia", hide_ui=False)
    assert result["layers"]["scene"]["filter"] == "sepia"
    assert result["layers"]["ui"]["filter"] == "none"


def test_high_resolution_photo_matches_preview_framing():
    sandbox = make()
    result = sandbox.take_photo(width=3840, height=2160)
    assert result["width"] == 3840
    assert result["height"] == 2160
    assert result["framing_aspect"] == pytest.approx(1920.0 / 1080.0)


def test_hiding_ui_removes_crosshair_and_health_bar():
    sandbox = make()
    hidden = sandbox.take_photo(hide_ui=True)
    assert hidden["layers"]["ui"]["elements"] == []
    shown = sandbox.take_photo(hide_ui=False)
    assert set(shown["layers"]["ui"]["elements"]) == {"crosshair", "health"}


def test_camera_cannot_enter_loaded_geometry():
    sandbox = make()
    sandbox.add_collider(0.0, 0.0, 0.0, 2.0, 2.0, 2.0)
    with pytest.raises(photo.PhotoModeError):
        sandbox.set_camera(1.0, 1.0, 1.0)


def test_illegal_fov_and_negative_resolution_raise():
    sandbox = make()
    with pytest.raises(photo.PhotoModeError):
        sandbox.set_camera(5.0, 5.0, 5.0, fov=0.0)
    with pytest.raises(photo.PhotoModeError):
        sandbox.set_resolution(-100, 100)


def test_resolution_change_rebuilds_the_offscreen_target():
    sandbox = make()
    first = sandbox.local_state()["target_id"]
    sandbox.set_resolution(2560, 1440)
    second = sandbox.local_state()["target_id"]
    assert second != first
    assert sandbox.local_state()["resolution"] == (2560, 1440)


def test_photo_metadata_survives_restart():
    sandbox = make()
    sandbox.take_photo()
    saved = sandbox.snapshot()
    restored = photo.PhotoModeSandbox()
    restored.restore(saved)
    saved_photos = restored.photos()
    assert len(saved_photos) == 1
    assert saved_photos[0]["location"] != ""
    assert saved_photos[0]["scene"] == "village"


def test_damage_during_photo_mode_is_reported():
    sandbox = make()
    sandbox.damage(10)
    assert sandbox.health() == 90
    assert sandbox.last_feedback() is not None


def test_photo_buffers_do_not_grow_with_each_shot():
    sandbox = make()
    sandbox.take_photo()
    after_one = sandbox.allocated_bytes()
    for _ in range(9):
        sandbox.take_photo()
    after_ten = sandbox.allocated_bytes()
    assert len(sandbox.photos()) == 10
    assert after_ten <= after_one


def test_teammates_do_not_see_a_frozen_character():
    sandbox = make()
    assert sandbox.local_state()["frozen"] is True
    assert sandbox.server_view()["frozen"] is False


def test_dungeon_progress_is_preserved_across_photo_mode():
    sandbox = make()
    sandbox.take_photo()
    sandbox.exit()
    assert sandbox.local_state()["progress"] == 3


def test_double_enter_and_exit_without_enter_raise():
    sandbox = make()
    with pytest.raises(photo.PhotoModeError):
        sandbox.enter(scene())
    sandbox.exit()
    with pytest.raises(photo.PhotoModeError):
        sandbox.exit()


def test_weather_is_restored_after_shooting():
    sandbox = make()
    sandbox.set_weather("noon")
    assert sandbox.local_state()["weather"] == "noon"
    sandbox.exit()
    assert sandbox.local_state()["weather"] == "dusk"
    assert sandbox.local_state()["time_ms"] == 100
