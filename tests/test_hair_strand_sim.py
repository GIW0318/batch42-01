import math

import pytest

import hair_strand_sim
from hair_strand_sim import HairStrandSim


def _positions_for_fps(fps):
    game = HairStrandSim()
    game.reset((0.0, 0.0, 0.0))
    game.simulate(1.0, fps)
    return game.strand_positions()


def test_fixed_substeps_make_pose_frame_rate_independent():
    pose_30 = _positions_for_fps(30)
    pose_60 = _positions_for_fps(60)
    pose_120 = _positions_for_fps(120)
    assert len(pose_30) == len(pose_60) == len(pose_120)
    for strand_30, strand_60, strand_120 in zip(pose_30, pose_60, pose_120):
        for axis in range(3):
            assert strand_30[axis] == pytest.approx(strand_60[axis], abs=1e-4)
            assert strand_30[axis] == pytest.approx(strand_120[axis], abs=1e-4)


def test_long_frame_is_capped_to_max_substeps():
    game = HairStrandSim(max_substeps=4)
    game.reset((0.0, 0.0, 0.0))
    game.step(1.0)
    report = game.budget_report()
    assert report["substeps_last_frame"] <= 4


def test_head_collision_does_not_penetrate():
    game = HairStrandSim()
    game.reset((0.0, 0.0, 0.0))
    game.set_collider((0.0, 0.0, 0.0), 1.0, shoulder_y=-1.0)
    game.simulate(0.5, 30)
    for strand in game.strand_positions():
        for point in strand:
            distance = math.sqrt(point[0] ** 2 + point[1] ** 2 + point[2] ** 2)
            assert distance >= 1.0 - 1e-3


def test_teleport_resets_history_and_does_not_stretch_strands():
    game = HairStrandSim()
    game.reset((0.0, 0.0, 0.0))
    game.simulate(1.0, 60)
    game.teleport((100.0, 0.0, 0.0))
    game.simulate(0.02, 60)
    for strand in game.strand_positions():
        for point in strand:
            distance = math.sqrt((point[0] - 100.0) ** 2
                                 + point[1] ** 2 + point[2] ** 2)
            assert distance <= 6.0


def test_low_lod_still_rejects_head_penetration():
    game = HairStrandSim()
    game.reset((0.0, 0.0, 0.0))
    game.set_collider((0.0, 0.0, 0.0), 1.0, shoulder_y=-1.0)
    game.set_lod(2)
    game.simulate(0.5, 60)
    for strand in game.strand_positions():
        for point in strand:
            distance = math.sqrt(point[0] ** 2 + point[1] ** 2 + point[2] ** 2)
            assert distance >= 1.0 - 1e-3


def test_same_wind_source_is_not_integrated_twice():
    game = HairStrandSim()
    game.set_wind("gust", (1.0, 0.0, 0.0))
    game.set_wind("gust", (1.0, 0.0, 0.0))
    assert game.wind_vector() == (1.0, 0.0, 0.0)


def test_long_simulation_never_produces_nan():
    game = HairStrandSim()
    game.reset((0.0, 0.0, 0.0))
    game.simulate(30.0, 30)
    for strand in game.strand_positions():
        for point in strand:
            assert all(math.isfinite(value) for value in point)


def test_many_characters_degrade_only_far_strands():
    game = HairStrandSim(budget_ms=2.0, strands=64)
    game.reset((0.0, 0.0, 0.0))
    game.set_character_count(20)
    game.step(1.0 / 60.0)
    report = game.budget_report()
    assert report["simulated_strands"] < report["requested_strands"]
    assert report["main_character_degraded"] is False


def test_saved_appearance_reloads_pose_without_fly_in():
    game = HairStrandSim()
    game.reset((0.0, 0.0, 0.0))
    game.simulate(0.5, 60)
    saved_pose = game.strand_positions()
    snapshot = game.save_appearance()

    restored = HairStrandSim()
    restored.load_appearance(snapshot)
    loaded_pose = restored.strand_positions()
    for before, after in zip(saved_pose, loaded_pose):
        for axis in range(3):
            assert before[axis] == pytest.approx(after[axis], abs=1e-6)
    assert snapshot["hairstyle"]


def test_low_frame_rate_does_not_pass_through_shoulder():
    game = HairStrandSim()
    game.reset((0.0, 0.0, 0.0))
    game.set_collider((0.0, 0.0, 0.0), 1.0, shoulder_y=-1.0)
    game.step(1.0 / 15.0)
    for strand in game.strand_positions():
        for point in strand:
            assert point[1] >= -1.0 - 1e-3


def test_switching_hairstyle_clears_collision_cache():
    game = HairStrandSim()
    game.reset((0.0, 0.0, 0.0))
    game.set_collider((0.0, 0.0, 0.0), 1.0, shoulder_y=-1.0)
    game.set_hairstyle("long")
    game.simulate(0.2, 60)
    game.set_hairstyle("short")
    assert game.collision_cache_size() == 0


def test_disabled_simulation_keeps_last_pose():
    game = HairStrandSim()
    game.reset((0.0, 0.0, 0.0))
    game.simulate(0.2, 60)
    pose = game.strand_positions()
    game.set_enabled(False)
    game.simulate(1.0, 60)
    assert game.strand_positions() == pose


def test_wind_amplitude_is_frame_rate_independent():
    def displacement(fps):
        game = HairStrandSim()
        game.reset((0.0, 0.0, 0.0))
        game.set_wind("breeze", (0.6, 0.0, 0.0))
        game.simulate(1.0, fps)
        first = game.strand_positions()[0][-1]
        return first[0]

    assert displacement(30) == pytest.approx(displacement(60), abs=1e-3)


def test_reset_clears_previous_map_displacement():
    game = HairStrandSim()
    game.reset((0.0, 0.0, 0.0))
    game.simulate(1.0, 60)
    game.reset((0.0, 0.0, 0.0))
    for strand in game.strand_positions():
        for point in strand:
            distance = math.sqrt(point[0] ** 2 + point[1] ** 2 + point[2] ** 2)
            assert distance <= 6.0


def test_simulation_is_deterministic_for_same_inputs():
    left = HairStrandSim()
    right = HairStrandSim()
    left.reset((1.0, 2.0, 3.0))
    right.reset((1.0, 2.0, 3.0))
    left.set_wind("gust", (0.4, -0.1, 0.0))
    right.set_wind("gust", (0.4, -0.1, 0.0))
    left.simulate(0.5, 60)
    right.simulate(0.5, 60)
    assert left.strand_positions() == right.strand_positions()
