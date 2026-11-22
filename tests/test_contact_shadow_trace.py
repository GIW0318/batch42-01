import pytest

import contact_shadow_trace as shadow

FLAT = [10.0] * 8
NORMALS = [(0.0, 0.0, 1.0)] * 8
LIGHT = (0.3, 0.9, 0.0)
CAMERA = (0.0, 0.0, 1.0)


def make(**kwargs):
    return shadow.ContactShadowTrace(**kwargs)


def test_invalid_step_parameters_raise():
    with pytest.raises(shadow.ContactShadowError):
        shadow.ContactShadowTrace(max_steps=0)
    with pytest.raises(shadow.ContactShadowError):
        shadow.ContactShadowTrace(step_px=0.0)
    with pytest.raises(shadow.ContactShadowError):
        shadow.ContactShadowTrace(thickness=-1.0)


def test_flat_surface_produces_no_contact_shadow():
    tracer = make()
    assert tracer.trace((0.5, 0.5), FLAT, NORMALS, LIGHT, CAMERA) == 0.0


def test_occluder_in_front_produces_occlusion():
    tracer = make()
    depth = [10.0, 10.0, 8.0, 8.0, 10.0, 10.0, 10.0, 10.0]
    assert tracer.trace((0.5, 0.5), depth, NORMALS, LIGHT, CAMERA) > 0.0


def test_occlusion_fades_beyond_max_distance():
    tracer = make(max_distance=50.0)
    assert tracer.sample_occlusion(200.0, 1.0, 30.0) == 0.0


def test_depth_discontinuity_stops_the_ray():
    tracer = make()
    depth = [10.0, 10.0, 2.0, 2.0, 2.0, 2.0, 2.0, 2.0]
    assert tracer.trace((0.5, 0.5), depth, NORMALS, LIGHT, CAMERA) == 0.0


def test_thin_geometry_still_casts_a_shadow():
    tracer = make(thickness=0.4)
    depth = [10.0, 10.0, 9.7, 9.7, 10.0, 10.0, 10.0, 10.0]
    assert tracer.trace((0.5, 0.5), depth, NORMALS, LIGHT, CAMERA) > 0.0


def test_occlusion_fades_with_distance():
    tracer = make(max_distance=500.0)
    near = tracer.sample_occlusion(1.0, 1.0, 30.0)
    far = tracer.sample_occlusion(50.0, 1.0, 30.0)
    assert near > far


def test_low_sun_angle_fades_the_shadow_out():
    tracer = make()
    assert tracer.sample_occlusion(5.0, 1.0, 1.0) < 0.05


def test_jitter_is_reproducible_for_a_frame_and_ignores_camera_motion():
    tracer = make()
    first = tracer.jitter(12)
    second = tracer.jitter(12)
    assert first == second
    assert first != tracer.jitter(13)
    first_trace = tracer.trace((0.5, 0.5), FLAT, NORMALS, LIGHT, CAMERA)
    second_trace = tracer.trace((0.5, 0.5), FLAT, NORMALS, LIGHT,
                               (-1.0, 0.0, 0.0))
    assert first_trace == second_trace


def test_budget_caps_steps_and_downsamples():
    tracer = make()
    tracer.set_budget(4)
    tracer.trace((0.5, 0.5), FLAT, NORMALS, LIGHT, CAMERA)
    stats = tracer.last_stats()
    assert stats["steps"] <= 4
    assert stats["downsampled"] is True


def test_both_vr_eyes_use_identical_parameters():
    tracer = make()
    assert tracer.params("left") == tracer.params("right")


def test_overlapping_shadows_clamp_instead_of_adding():
    tracer = make()
    assert tracer.combine(0.8, 0.8) == pytest.approx(0.8)


def test_airborne_character_loses_contact_shadow():
    tracer = make()
    assert tracer.trace((0.5, 0.5), FLAT, NORMALS, LIGHT, CAMERA,
                        height=5.0) == 0.0


def test_holes_in_the_mask_do_not_cast_solid_shadow():
    tracer = make()
    depth = [10.0, 10.0, 8.0, 8.0, 10.0, 10.0, 10.0, 10.0]
    assert tracer.trace((0.5, 0.5), depth, NORMALS, LIGHT, CAMERA,
                        mask=0.0) == 0.0


def test_camera_rotation_does_not_move_the_shadow():
    tracer = make()
    depth = [10.0, 10.0, 8.0, 8.0, 10.0, 10.0, 10.0, 10.0]
    front = tracer.trace((0.5, 0.5), depth, NORMALS, LIGHT, CAMERA)
    side = tracer.trace((0.5, 0.5), depth, NORMALS, LIGHT, (1.0, 0.0, 0.0))
    assert front == side
