import pytest

import motor_slope_contact as ms


def make():
    body = ms.SlopeContact(step_ms=10.0)
    body.set_ground_segment(0.0, 0.0, 100.0, 0.0)
    return body


def test_fixed_step_consumes_accumulated_frame_time():
    body = make()
    body.update(35)
    state = body.state()
    assert state["steps"] == 3
    assert state["accumulator_ms"] == pytest.approx(5.0)
    body.update(5)
    assert body.state()["steps"] == 4


def test_jump_apex_is_identical_at_30_60_and_120_fps():
    apexes = []
    for frame_ms in (33.0, 16.0, 8.0):
        body = make()
        body.update(200)
        body.set_input(0.0, True)
        body.update(frame_ms)
        best = body.state()["y"]
        for _ in range(60):
            body.update(frame_ms)
            best = max(best, body.state()["y"])
        apexes.append(best)
    assert max(apexes) - min(apexes) < 0.01


def test_repeated_jump_press_in_same_frame_applies_once():
    body = make()
    body.update(100)
    body.set_input(0.0, True)
    body.set_input(0.0, True)
    body.set_input(0.0, True)
    body.update(10)
    velocity = body.state()["vy"]
    assert 4.0 < velocity < 6.1


def test_landing_buffer_expires_after_one_hundred_milliseconds():
    body = make()
    body.buffer_jump(1000)
    assert body.consume_jump_buffer(1100) is True
    body.buffer_jump(2000)
    assert body.consume_jump_buffer(2101) is False


def test_character_does_not_slide_on_walkable_slope():
    body = ms.SlopeContact(step_ms=10.0)
    body.set_ground_segment(0.0, 0.0, 20.0, 7.27)
    body.update(500)
    start = body.state()["x"]
    body.update(1000)
    assert abs(body.state()["x"] - start) < 0.05


def test_slope_steeper_than_limit_raises():
    body = ms.SlopeContact(step_ms=10.0, max_slope_deg=45.0)
    body.set_ground_segment(0.0, 0.0, 10.0, 20.0)
    with pytest.raises(ms.ContactSolveError):
        body.update(10)


def test_degenerate_ground_normal_raises():
    body = make()
    body.set_ground_normal((0.0, 0.0))
    with pytest.raises(ms.ContactSolveError):
        body.update(10)


def test_moving_platform_carries_character_and_keeps_local_offset():
    body = ms.SlopeContact(step_ms=10.0)
    body.add_box(0.0, 0.0, 20.0, 1.0, dynamic=True)
    body.update(200)
    before = body.state()
    body.set_platform_motion(5.0, 0.0)
    body.update(10)
    after = body.state()
    assert after["x"] - before["x"] == pytest.approx(5.0, abs=0.2)
    assert after["y"] == pytest.approx(before["y"], abs=0.05)


def test_ceiling_hit_clears_upward_velocity_without_rebound():
    body = make()
    body.set_ceiling(2.0)
    body.update(100)
    body.set_input(0.0, True)
    top = body.state()["y"]
    for _ in range(80):
        body.update(10)
        top = max(top, body.state()["y"])
        assert body.state()["vy"] <= 0.01 or body.state()["y"] < 2.0
    assert top <= 2.0 + 0.15


def test_dynamic_box_counts_as_ground():
    body = ms.SlopeContact(step_ms=10.0)
    body.add_box(0.0, 0.0, 20.0, 1.0, dynamic=True)
    body.update(300)
    assert body.state()["grounded"] is True
    assert body.state()["y"] >= 1.0 - 0.05


def test_dash_does_not_pass_through_thin_wall():
    body = ms.SlopeContact(step_ms=10.0)
    body.set_ground_segment(0.0, 0.0, 100.0, 0.0)
    body.add_box(1.0, 0.0, 0.2, 5.0)
    body.update(100)
    body.request_dash(5.0)
    body.update(20)
    assert body.state()["x"] <= 1.0 + 0.01


def test_cannot_stand_up_under_low_ceiling():
    body = ms.SlopeContact(step_ms=10.0)
    body.set_ground_segment(0.0, 0.0, 100.0, 0.0)
    body.set_ceiling(0.9)
    body.update(100)
    assert body.set_crouch(True) is True
    assert body.set_crouch(False) is False
    assert body.state()["crouched"] is True


def test_corner_pinch_does_not_launch_character_out_of_bounds():
    body = ms.SlopeContact(step_ms=10.0)
    body.set_ground_segment(0.0, 0.0, 100.0, 0.0)
    body.add_box(-0.2, 0.0, 0.2, 5.0)
    body.add_box(0.4, 0.0, 0.2, 5.0)
    body.update(300)
    position = body.state()
    assert -0.2 <= position["x"] <= 0.4
    assert position["y"] >= 0.0


def test_wall_hugging_jumps_do_not_climb_the_wall():
    body = ms.SlopeContact(step_ms=10.0)
    body.set_ground_segment(0.0, 0.0, 100.0, 0.0)
    body.add_box(0.6, 0.0, 0.5, 5.0)
    body.update(100)
    best = body.state()["y"]
    for _ in range(3):
        body.set_input(0.0, True)
        body.update(300)
        best = max(best, body.state()["y"])
    assert best < 2.2


def test_final_position_stays_outside_every_contact_surface():
    body = ms.SlopeContact(step_ms=10.0)
    body.set_ground_segment(0.0, 0.0, 100.0, 0.0)
    body.update(200)
    state = body.state()
    assert state["grounded"] is True
    assert state["y"] >= 0.0
