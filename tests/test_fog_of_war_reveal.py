import pytest

import fog_of_war_reveal as fog


def make(**kwargs):
    reveal = fog.FogReveal(64, 64, **kwargs)
    reveal.add_team("red")
    reveal.add_team("blue")
    return reveal


def test_visible_cells_clear_after_unit_leaves_but_explored_remains():
    reveal = make()
    reveal.add_unit("scout", "red", 5, 5, 1)
    reveal.update(1)
    assert (6, 5) in reveal.visible("red")
    reveal.remove_unit("scout")
    reveal.update(2)
    assert (6, 5) not in reveal.visible("red")
    assert (6, 5) in reveal.explored("red")


def test_explored_and_current_visibility_are_separate_sets():
    reveal = make()
    reveal.add_unit("scout", "red", 5, 5, 2)
    reveal.update(1)
    before = set(reveal.explored("red"))
    reveal.remove_unit("scout")
    reveal.update(2)
    assert set(reveal.explored("red")) == before
    assert reveal.visible("red") == set()


def test_one_team_vision_never_leaks_to_the_other_team():
    reveal = make()
    reveal.add_unit("red_scout", "red", 2, 2, 1)
    reveal.add_unit("blue_scout", "blue", 30, 30, 1)
    reveal.update(1)
    assert (30, 30) not in reveal.visible("red")
    assert (2, 2) not in reveal.visible("blue")


def test_terrain_blocker_stops_line_of_sight():
    reveal = make()
    reveal.add_unit("scout", "red", 1, 1, 3)
    reveal.add_blocker(2, 1)
    reveal.update(1)
    assert (3, 1) not in reveal.visible("red")
    assert (1, 2) in reveal.visible("red")


def test_invisible_unit_is_not_listed_to_enemy():
    reveal = make()
    reveal.add_unit("spy", "blue", 2, 1, 1, invisible=True)
    reveal.add_unit("guard", "blue", 2, 2, 1, invisible=False)
    reveal.add_unit("scout", "red", 1, 1, 3)
    reveal.update(1)
    seen = reveal.visible_units("red")
    assert "spy" not in seen
    assert "guard" in seen


def test_destroyed_scouting_building_stops_providing_vision():
    reveal = make()
    reveal.add_unit("tower", "red", 10, 10, 3)
    reveal.update(1)
    assert (12, 10) in reveal.visible("red")
    reveal.remove_unit("tower")
    reveal.update(2)
    assert (12, 10) not in reveal.visible("red")


def test_stale_visibility_version_is_discarded():
    reveal = make()
    reveal.add_unit("scout", "red", 5, 5, 1)
    reveal.update(1)
    assert reveal.visible("red", version=1) is not None
    reveal.update(2)
    assert reveal.visible("red", version=1) is None


def test_duplicate_submission_for_same_frame_does_not_expand_twice():
    reveal = make()
    reveal.add_unit("scout", "red", 5, 5, 2)
    first = reveal.update(7)
    second = reveal.update(7)
    assert set(first) == set(second)
    assert reveal.visible("red", version=1) is not None


def test_snapshot_restores_explored_map_and_alive_units():
    reveal = make()
    reveal.add_unit("scout", "red", 5, 5, 2)
    reveal.add_unit("spare", "red", 20, 20, 1)
    reveal.update(1)
    reveal.remove_unit("spare")
    saved = reveal.snapshot()
    restored = make()
    restored.restore(saved)
    assert (6, 5) in restored.explored("red")
    assert (6, 5) in restored.visible("red")
    restored.update(2)
    assert (21, 20) not in restored.explored("red")


def test_unknown_team_query_raises_vision_denied():
    reveal = make()
    with pytest.raises(fog.VisionDeniedError):
        reveal.visible("green")


def test_spectator_cannot_read_another_teams_vision():
    reveal = make()
    reveal.add_unit("scout", "red", 5, 5, 1)
    reveal.update(1)
    with pytest.raises(fog.VisionDeniedError):
        reveal.visible("red", viewer="spectator")


def test_budget_slows_reveal_but_results_stay_monotonic():
    reveal = make()
    reveal.add_unit("scout", "red", 5, 5, 2)
    reveal.set_budget(1)
    previous = set()
    for frame in range(1, 30):
        reveal.update(frame)
        current = set(reveal.explored("red"))
        assert previous <= current
        previous = current
    assert len(reveal.explored("red")) == 25


def test_vision_is_clipped_to_the_terrain_grid():
    reveal = make()
    reveal.add_unit("scout", "red", 0, 0, 2)
    reveal.update(1)
    cells = reveal.visible("red")
    assert (0, 0) in cells
    assert all(x >= 0 and y >= 0 for x, y in cells)


def test_contested_cell_depends_on_teams_not_insertion_order():
    first = make()
    first.add_unit("r", "red", 5, 5, 1)
    first.add_unit("b", "blue", 5, 5, 1)
    first.update(1)
    second = make()
    second.add_unit("b", "blue", 5, 5, 1)
    second.add_unit("r", "red", 5, 5, 1)
    second.update(1)
    assert first.visible("red") == second.visible("red")
    assert first.visible("blue") == second.visible("blue")


def test_repeated_update_without_changes_is_stable():
    reveal = make()
    reveal.add_unit("scout", "red", 5, 5, 2)
    reveal.update(1)
    first = set(reveal.visible("red"))
    reveal.update(2)
    second = set(reveal.visible("red"))
    assert first == second
