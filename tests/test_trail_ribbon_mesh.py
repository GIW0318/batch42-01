import pytest

import trail_ribbon_mesh as trail

MATRIX = ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0))


def make(**kwargs):
    return trail.TrailRibbon(**kwargs)


def test_duplicate_timestamp_keeps_the_last_sample():
    ribbon = make()
    ribbon.add_sample(0.0, MATRIX, (0.0, 0.0, 0.0))
    ribbon.add_sample(0.0, MATRIX, (1.0, 0.0, 0.0))
    built = ribbon.build(0.0)
    assert len(built["samples"]) == 1
    assert built["samples"][0]["position"] == (1.0, 0.0, 0.0)


def test_timestamp_regression_raises():
    ribbon = make()
    ribbon.add_sample(10.0, MATRIX, (0.0, 0.0, 0.0))
    with pytest.raises(trail.TrailError):
        ribbon.add_sample(5.0, MATRIX, (1.0, 0.0, 0.0))


def test_missing_bone_matrix_raises():
    ribbon = make()
    with pytest.raises(trail.TrailError):
        ribbon.add_sample(0.0, None, (0.0, 0.0, 0.0))


def test_negative_segment_configuration_raises():
    with pytest.raises(trail.TrailError):
        make(max_segments=-1)


def test_segment_overflow_recycles_the_oldest_segment():
    ribbon = make(max_segments=64)
    for index in range(100):
        ribbon.add_sample(float(index), MATRIX, (float(index), 0.0, 0.0))
    built = ribbon.build(99.0)
    assert len(built["samples"]) == 64
    assert built["samples"][0]["timestamp"] > 0.0


def test_expired_samples_are_released():
    ribbon = make(lifetime_ms=300)
    ribbon.add_sample(0.0, MATRIX, (0.0, 0.0, 0.0))
    ribbon.add_sample(10.0, MATRIX, (1.0, 0.0, 0.0))
    built = ribbon.build(1000.0)
    assert built["samples"] == []
    assert ribbon.pool_stats()["vertex_count"] == 0


def test_duplicate_positions_never_create_zero_area_triangles():
    ribbon = make()
    for index in range(4):
        ribbon.add_sample(float(index), MATRIX, (5.0, 0.0, 0.0))
    built = ribbon.build(3.0)
    assert all(area > 0.0 for area in built["triangles"])


def test_vertex_pool_is_reused_between_builds():
    ribbon = make()
    for index in range(10):
        ribbon.add_sample(float(index), MATRIX, (float(index), 0.0, 0.0))
    ribbon.build(9.0)
    first = ribbon.pool_stats()
    ribbon.build(9.0)
    second = ribbon.pool_stats()
    assert first["allocations"] == 1
    assert second["allocations"] == 1
    assert first["vertex_count"] == second["vertex_count"]


def test_pause_stops_sampling():
    ribbon = make()
    ribbon.set_paused(True)
    assert ribbon.add_sample(0.0, MATRIX, (0.0, 0.0, 0.0)) is False
    assert ribbon.samples() == []


def test_resume_starts_from_the_current_bone_position():
    ribbon = make()
    ribbon.set_paused(True)
    ribbon.add_sample(0.0, MATRIX, (0.0, 0.0, 0.0))
    ribbon.add_sample(10.0, MATRIX, (1.0, 0.0, 0.0))
    ribbon.set_paused(False)
    ribbon.add_sample(20.0, MATRIX, (5.0, 5.0, 0.0))
    assert ribbon.samples()[-1]["position"] == (5.0, 5.0, 0.0)


def test_uv_coordinates_run_from_zero_to_one_across_the_ribbon():
    ribbon = make()
    for index in range(5):
        ribbon.add_sample(float(index), MATRIX, (float(index), 0.0, 0.0))
    built = ribbon.build(4.0)
    us = {vertex["uv"][0] for vertex in built["vertices"]}
    vs = [vertex["uv"][1] for vertex in built["vertices"]]
    assert us == {0.0, 1.0}
    assert min(vs) >= 0.0
    assert max(vs) <= 1.0


def test_transparent_segments_are_sorted_far_to_near():
    ribbon = make()
    for index in range(4):
        ribbon.add_sample(float(index), MATRIX, (float(index), 0.0, 0.0))
    ordered = ribbon.sort_indices((0.0, 0.0, 10.0))
    distances = [abs(index - 0.0) for index in ordered]
    assert distances == sorted(distances, reverse=True)


def test_material_is_per_instance():
    first = make()
    second = make()
    first.set_material((1.0, 0.0, 0.0, 1.0))
    second.set_material((0.0, 0.0, 1.0, 1.0))
    assert first.material() != second.material()


def test_segment_length_is_capped_after_a_teleport():
    ribbon = make(max_segment_length=10.0)
    ribbon.add_sample(0.0, MATRIX, (0.0, 0.0, 0.0))
    ribbon.add_sample(10.0, MATRIX, (100.0, 0.0, 0.0))
    built = ribbon.build(10.0)
    lengths = built["segment_lengths"]
    assert all(length <= 10.0 for length in lengths)


def test_samples_are_resampled_to_a_common_time_base():
    ribbon = make(time_base_ms=10.0)
    ribbon.add_sample(0.0, MATRIX, (0.0, 0.0, 0.0))
    ribbon.add_sample(100.0, MATRIX, (100.0, 0.0, 0.0))
    built = ribbon.build(100.0)
    stamps = [sample["timestamp"] for sample in built["samples"]]
    assert len(stamps) >= 11
    assert all(stamp % 10.0 == 0.0 for stamp in stamps)
