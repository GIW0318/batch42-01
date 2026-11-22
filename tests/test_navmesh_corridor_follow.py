import pytest

import navmesh_corridor_follow as nav

POLYS = {
    "A": [(0, 0), (10, 0), (10, 10), (0, 10)],
    "B": [(10, 0), (20, 0), (20, 10), (10, 10)],
    "C": [(20, 0), (30, 0), (30, 10), (20, 10)],
}


def make():
    cf = nav.CorridorFollow()
    cf.load_navmesh(1, POLYS)
    return cf


def test_path_carries_navmesh_version():
    cf = make()
    path = cf.find_path((1, 1), (9, 9))
    assert path["version"] == 1
    assert tuple(path["points"][0]) == (1, 1)
    assert tuple(path["points"][-1]) == (9, 9)


def test_path_from_old_version_is_rejected_after_rebuild():
    cf = make()
    path = cf.find_path((1, 1), (25, 5))
    cf.load_navmesh(2, POLYS)
    with pytest.raises(nav.NavMeshQueryError):
        cf.follow("u1", (1, 1), path)


def test_goal_inside_blocked_polygon_reports_cell():
    cf = make()
    cf.set_walkable("C", False)
    with pytest.raises(nav.NavMeshQueryError) as info:
        cf.find_path((5, 5), (25, 5))
    assert tuple(info.value.cell) == (25, 5)


def test_closed_door_blocks_path_and_opening_it_allows_reroute():
    cf = make()
    cf.set_walkable("B", False)
    with pytest.raises(nav.NavMeshQueryError):
        cf.find_path((5, 5), (25, 5))
    cf.set_walkable("B", True)
    path = cf.find_path((5, 5), (25, 5))
    assert "C" in path["polygons"]


def test_off_mesh_position_is_snapped_back():
    cf = make()
    path = cf.find_path((5, 5), (25, 5))
    result = cf.follow("u1", (50, 50), path)
    assert tuple(result["point"]) != (50, 50)
    assert cf.polygon_at(result["point"]) is not None


def test_one_way_jump_link_is_not_used_in_reverse():
    cf = nav.CorridorFollow()
    cf.load_navmesh(1, {
        "A": [(0, 0), (10, 0), (10, 10), (0, 10)],
        "B": [(30, 0), (40, 0), (40, 10), (30, 10)],
    })
    cf.add_link("jump", "A", "B", one_way=True)
    forward = cf.find_path((5, 5), (35, 5))
    assert "B" in forward["polygons"]
    with pytest.raises(nav.NavMeshQueryError):
        cf.find_path((35, 5), (5, 5))


def test_avoidance_push_never_leaves_walkable_polygon():
    cf = make()
    cf.find_path((5, 5), (9, 9))
    snapped = cf.apply_avoidance("u1", (9, 9), (100, 100))
    assert cf.polygon_at(snapped) is not None


def test_async_rebuild_keeps_old_version_until_commit():
    cf = make()
    cf.begin_rebuild(2)
    old_path = cf.find_path((5, 5), (25, 5))
    assert old_path["version"] == 1
    assert "C" in old_path["polygons"]
    cf.commit_rebuild(POLYS)
    new_path = cf.find_path((5, 5), (25, 5))
    assert new_path["version"] == 2


def test_paths_are_not_shared_between_units():
    cf = make()
    first = cf.find_path((5, 5), (25, 5))
    second = cf.find_path((5, 5), (25, 5))
    assert first is not second
    first["goal"] = (0, 0)
    assert tuple(second["goal"]) == (25, 5)


def test_unreachable_goal_raises_instead_of_empty_path():
    cf = nav.CorridorFollow()
    cf.load_navmesh(1, {
        "A": [(0, 0), (10, 0), (10, 10), (0, 10)],
        "E": [(100, 100), (110, 100), (110, 110), (100, 110)],
    })
    with pytest.raises(nav.NavMeshQueryError) as info:
        cf.find_path((5, 5), (105, 105))
    assert tuple(info.value.cell) == (105, 105)


def test_path_index_never_moves_backwards_when_unit_turns():
    cf = make()
    path = cf.find_path((5, 5), (25, 5))
    ahead = cf.follow("u1", (12, 5), path)["index"]
    behind = cf.follow("u1", (4, 5), path)["index"]
    assert behind >= ahead


def test_duplicate_replan_requests_are_coalesced_per_unit():
    cf = make()
    cf.find_path((5, 5), (25, 5))
    cf.request_replan("u1", (25, 5))
    cf.request_replan("u1", (25, 6))
    cf.request_replan("u1", (25, 5))
    processed = cf.process_replans()
    assert len(processed) == 1
    assert processed[0]["agent_id"] == "u1"
    assert tuple(processed[0]["path"]["goal"]) == (25, 5)


def test_replans_are_capped_at_eight_per_frame_and_queued():
    cf = make()
    cf.find_path((5, 5), (25, 5))
    for index in range(12):
        cf.request_replan("u%d" % index, (25, 5))
    first = cf.process_replans()
    second = cf.process_replans()
    assert len(first) == 8
    assert len(second) == 4


def test_restart_resumes_following_without_repathing():
    cf = make()
    path = cf.find_path((5, 5), (25, 5))
    cf.follow("u1", (12, 5), path)
    saved = cf.save_state()
    restarted = nav.CorridorFollow()
    restarted.load_navmesh(1, POLYS)
    restarted.load_state(saved)
    resumed = restarted.follow("u1", (13, 5))
    assert resumed["index"] >= 1
    assert resumed["arrived"] is False


def test_every_path_point_sits_on_a_walkable_polygon():
    cf = make()
    path = cf.find_path((1, 1), (29, 9))
    assert len(path["points"]) >= 2
    for point in path["points"]:
        assert cf.polygon_at(point) is not None
