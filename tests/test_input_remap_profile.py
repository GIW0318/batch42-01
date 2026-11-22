import pytest

import input_remap_profile
from input_remap_profile import BindingConflict, InputError, InputRemapProfile


def test_same_physical_input_cannot_bind_two_actions():
    game = InputRemapProfile()
    game.bind("keyboard", "W", "move_forward")
    with pytest.raises(BindingConflict) as exc:
        game.bind("keyboard", "W", "move_backward")
    assert exc.value.code == "binding_conflict"
    assert game.resolve("keyboard", [("W", 0.0)]) == ["move_forward"]


def test_one_action_can_use_several_physical_inputs():
    game = InputRemapProfile()
    game.bind("keyboard", "W", "move_forward")
    game.bind("keyboard", "UP", "move_forward")
    assert game.resolve("keyboard", [("W", 0.0)]) == ["move_forward"]
    assert game.resolve("keyboard", [("UP", 0.0)]) == ["move_forward"]


def test_combo_takes_priority_over_single_bindings():
    game = InputRemapProfile(combo_window=0.08)
    game.bind("keyboard", "A", "strafe_left")
    game.bind("keyboard", "B", "strafe_right")
    game.bind_combo("keyboard", ("A", "B"), "dash")
    resolved = game.resolve("keyboard", [("A", 0.0), ("B", 0.02)])
    assert resolved == ["dash"]


def test_outside_combo_window_falls_back_to_single_bindings():
    game = InputRemapProfile(combo_window=0.08)
    game.bind("keyboard", "A", "strafe_left")
    game.bind("keyboard", "B", "strafe_right")
    game.bind_combo("keyboard", ("A", "B"), "dash")
    resolved = game.resolve("keyboard", [("A", 0.0), ("B", 0.5)])
    assert "dash" not in resolved
    assert "strafe_left" in resolved
    assert "strafe_right" in resolved


def test_repeated_combo_events_trigger_combo_once():
    game = InputRemapProfile(combo_window=0.08)
    game.bind("keyboard", "A", "strafe_left")
    game.bind("keyboard", "B", "strafe_right")
    game.bind_combo("keyboard", ("A", "B"), "dash")
    resolved = game.resolve("keyboard", [
        ("A", 0.00), ("B", 0.01), ("A", 0.02), ("B", 0.03)])
    assert resolved.count("dash") == 1


def test_hotplug_restores_profile_by_guid():
    game = InputRemapProfile()
    game.device_connected("guid-1", "gamepad")
    game.set_deadzone("gamepad", 0.3)
    game.device_disconnected("guid-1")
    result = game.device_connected("guid-1", "gamepad")
    assert result["status"] == "connected"
    assert game.deadzone("gamepad") == pytest.approx(0.3)


def test_new_guid_requires_rebinding_instead_of_silent_default():
    game = InputRemapProfile()
    game.device_connected("guid-1", "gamepad")
    game.device_disconnected("guid-1")
    result = game.device_connected("guid-2", "gamepad")
    assert result["status"] == "needs_rebind"


def test_deadzone_survives_save_and_load():
    game = InputRemapProfile()
    game.set_deadzone("gamepad", 0.25)
    blob = game.save_profile()
    restored = InputRemapProfile()
    restored.load_profile(blob)
    assert restored.deadzone("gamepad") == pytest.approx(0.25)


def test_deadzone_filters_small_axis_values():
    game = InputRemapProfile(default_deadzone=0.15)
    game.bind("gamepad", "LX", "move_x")
    game.set_deadzone("gamepad", 0.2)
    assert game.resolve("gamepad", [("LX", 0.0, 0.1)]) == []
    assert game.resolve("gamepad", [("LX", 0.0, 0.3)]) == ["move_x"]


def test_cloud_sync_is_idempotent():
    game = InputRemapProfile()
    game.bind("keyboard", "W", "move_forward")
    game.bind("keyboard", "SPACE", "jump")
    remote = game.save_profile()
    before = game.binding_count()
    game.sync_cloud(remote)
    game.sync_cloud(remote)
    assert game.binding_count() == before


def test_incompatible_profile_version_rejects_whole_file():
    game = InputRemapProfile()
    game.bind("keyboard", "W", "move_forward")
    before = game.binding_count()
    bad = game.save_profile()
    bad["profile_version"] = 99
    with pytest.raises(InputError) as exc:
        game.load_profile(bad)
    assert exc.value.code == "unsupported_profile_version"
    assert game.binding_count() == before
    assert game.resolve("keyboard", [("W", 0.0)]) == ["move_forward"]


def test_game_override_does_not_change_global_binding():
    game = InputRemapProfile()
    game.bind("keyboard", "W", "move_forward")
    game.set_game_override("game-x", {"W": "jump"})
    assert game.resolve_game("game-x", "keyboard", [("W", 0.0)]) == ["jump"]
    assert game.resolve("keyboard", [("W", 0.0)]) == ["move_forward"]
    game.clear_game_override("game-x")
    assert game.resolve_game("game-x", "keyboard", [("W", 0.0)]) == [
        "move_forward"]


def test_keyboard_and_gamepad_bindings_are_independent():
    game = InputRemapProfile()
    game.bind("keyboard", "A", "strafe_left")
    game.bind("gamepad", "A", "attack")
    assert game.resolve("keyboard", [("A", 0.0)]) == ["strafe_left"]
    assert game.resolve("gamepad", [("A", 0.0)]) == ["attack"]
    assert game.last_device() == "gamepad"


def test_combo_uses_event_time_not_net_arrival_time():
    game = InputRemapProfile(combo_window=0.08)
    game.bind("keyboard", "A", "strafe_left")
    game.bind("keyboard", "B", "strafe_right")
    game.bind_combo("keyboard", ("A", "B"), "dash")
    resolved = game.resolve("keyboard", [
        ("A", 0.00, 1.0, 0.50), ("B", 0.02, 1.0, 0.90)])
    assert resolved == ["dash"]


def test_bluetooth_and_wired_share_one_controller_identity():
    game = InputRemapProfile()
    game.device_connected("bt-guid", "gamepad", hardware_id="ctrl-1")
    game.device_connected("usb-guid", "gamepad", hardware_id="ctrl-1")
    assert game.device_count() == 1
