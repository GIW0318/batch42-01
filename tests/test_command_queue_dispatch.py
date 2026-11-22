import command_queue_dispatch as cd


def move(x, y):
    return {"type": "move", "target": (x, y)}


def make(**kwargs):
    dispatch = cd.CommandDispatch(**kwargs)
    dispatch.add_unit("u1", "p1", unit_type="tank")
    dispatch.add_unit("u2", "p1", unit_type="rifle")
    dispatch.add_unit("u3", "p2", unit_type="rifle")
    return dispatch


def test_commands_in_the_same_frame_keep_submission_order():
    dispatch = make()
    dispatch.issue("p1", "u1", move(1, 1), client_seq=10, frame=5)
    dispatch.issue("p1", "u1", move(2, 2), client_seq=11, frame=5)
    dispatch.issue("p1", "u1", move(3, 3), client_seq=12, frame=5)
    targets = [entry["target"] for entry in dispatch.queue("u1")]
    assert targets == [(1, 1), (2, 2), (3, 3)]


def test_duplicate_client_sequence_executes_once():
    dispatch = make()
    dispatch.issue("p1", "u1", move(1, 1), client_seq=10, frame=1)
    dispatch.issue("p1", "u1", move(1, 1), client_seq=10, frame=1)
    assert len(dispatch.queue("u1")) == 1


def test_queue_full_rejects_new_command_without_touching_queue():
    dispatch = make(max_queue=2)
    dispatch.issue("p1", "u1", move(1, 1), client_seq=1, frame=1)
    dispatch.issue("p1", "u1", move(2, 2), client_seq=2, frame=1)
    result = dispatch.issue("p1", "u1", move(3, 3), client_seq=3, frame=1)
    assert result["code"] == "QUEUE_FULL"
    assert len(dispatch.queue("u1")) == 2


def test_cancel_last_keeps_the_earlier_commands():
    dispatch = make()
    for index in range(3):
        dispatch.issue("p1", "u1", move(index, 0), client_seq=index, frame=1)
    assert dispatch.cancel_last("u1") is True
    targets = [entry["target"] for entry in dispatch.queue("u1")]
    assert targets == [(0, 0), (1, 0)]


def test_stop_clears_the_whole_queue():
    dispatch = make()
    dispatch.issue("p1", "u1", move(1, 1), client_seq=1, frame=1)
    dispatch.issue("p1", "u1", move(2, 2), client_seq=2, frame=1)
    dispatch.stop("u1")
    assert dispatch.queue("u1") == []


def test_dead_unit_command_returns_dead_unit_code():
    dispatch = make()
    dispatch.kill_unit("u1")
    result = dispatch.issue("p1", "u1", move(1, 1), client_seq=1, frame=1)
    assert result["code"] == "DEAD_UNIT"
    assert dispatch.queue("u1") == []


def test_missing_attack_target_returns_invalid_target_code():
    dispatch = make()
    result = dispatch.issue("p1", "u1", {"type": "attack", "target": "ghost"},
                            client_seq=1, frame=1)
    assert result["code"] == "INVALID_TARGET"
    assert dispatch.queue("u1") == []


def test_build_with_insufficient_resources_is_not_queued():
    dispatch = make()
    dispatch.set_resources("p1", 100)
    result = dispatch.issue("p1", "u1",
                            {"type": "build", "target": "barracks",
                             "cost": 500},
                            client_seq=1, frame=1)
    assert result["code"] == "INSUFFICIENT_RESOURCES"
    assert dispatch.queue("u1") == []


def test_frame_budget_caps_executed_commands_and_keeps_the_rest():
    dispatch = cd.CommandDispatch(frame_budget=40)
    for index in range(200):
        unit = "u%d" % index
        dispatch.add_unit(unit, "p1")
        dispatch.issue("p1", unit, move(index, 0), client_seq=index, frame=7)
    executed = dispatch.process_frame(7)
    assert len(executed) == 40
    remaining = sum(len(dispatch.queue("u%d" % index)) for index in range(200))
    assert remaining == 160
    while remaining:
        done = dispatch.process_frame(7)
        assert done
        remaining -= len(done)
    assert remaining == 0


def test_formation_assigns_unique_positions():
    dispatch = make()
    positions = dispatch.form_up(["u1", "u2", "u3"], anchor=(10, 10))
    points = list(positions.values())
    assert len(points) == 3
    assert len(set(points)) == 3


def test_selection_skips_units_hidden_by_fog():
    dispatch = make()
    selected = dispatch.select("p1", ["u1", "u2", "u3"], visible={"u1", "u3"})
    assert selected == ["u1"]


def test_rally_point_is_applied_to_new_spawned_units():
    dispatch = make()
    dispatch.set_rally((5, 5))
    dispatch.spawn("rifle", unit_id="u9")
    queued = dispatch.queue("u9")
    assert queued[0]["type"] == "move"
    assert queued[0]["target"] == (5, 5)


def test_snapshot_restores_each_units_queue():
    dispatch = make()
    dispatch.issue("p1", "u1", move(1, 1), client_seq=1, frame=3)
    dispatch.issue("p1", "u1", move(2, 2), client_seq=2, frame=3)
    saved = dispatch.snapshot()
    restored = cd.CommandDispatch()
    restored.restore(saved)
    targets = [entry["target"] for entry in restored.queue("u1")]
    assert targets == [(1, 1), (2, 2)]


def test_replace_queue_is_atomic_when_a_command_is_invalid():
    dispatch = make()
    dispatch.issue("p1", "u1", move(1, 1), client_seq=1, frame=1)
    dispatch.issue("p1", "u1", move(2, 2), client_seq=2, frame=1)
    result = dispatch.replace_queue("u1", [move(9, 9), {"type": "teleport"}])
    assert result["code"] == "INVALID_COMMAND"
    targets = [entry["target"] for entry in dispatch.queue("u1")]
    assert targets == [(1, 1), (2, 2)]


def test_unknown_command_type_is_rejected():
    dispatch = make()
    result = dispatch.issue("p1", "u1", {"type": "teleport"}, client_seq=1,
                            frame=1)
    assert result["code"] == "INVALID_COMMAND"
    assert dispatch.queue("u1") == []
