import pytest

import light_cookie_atlas as cookie

COOKIE = [[(1.0, 1.0, 1.0), (1.0, 1.0, 1.0)],
          [(1.0, 1.0, 1.0), (1.0, 1.0, 1.0)]]


def make():
    atlas = cookie.CookieAtlas(max_lights=4)
    atlas.set_cookie("leaves", COOKIE)
    return atlas


def test_each_light_gets_a_stable_unique_slot():
    atlas = make()
    first = atlas.register_light("l1", "leaves")
    second = atlas.register_light("l2", "leaves")
    again = atlas.register_light("l1", "leaves")
    assert first != second
    assert again == first
    assert atlas.slot_of("l1") == first


def test_slot_exhaustion_raises():
    atlas = make()
    for index in range(4):
        atlas.register_light("l%d" % index, "leaves")
    with pytest.raises(cookie.CookieAtlasError):
        atlas.register_light("l4", "leaves")


def test_cookie_size_mismatch_raises():
    atlas = make()
    atlas.set_cookie("small", [[(1.0, 1.0, 1.0)]])
    with pytest.raises(cookie.CookieAtlasError):
        atlas.register_light("l1", "small", size=256)


def test_invalid_light_id_raises():
    atlas = make()
    with pytest.raises(cookie.CookieAtlasError):
        atlas.register_light("", "leaves")


def test_release_frees_the_slot_immediately():
    atlas = make()
    atlas.register_light("l1", "leaves")
    atlas.release_light("l1")
    assert atlas.slot_of("l1") is None
    assert atlas.register_light("l2", "leaves") is not None


def test_rotating_the_light_changes_the_projection_matrix():
    atlas = make()
    atlas.register_light("l1", "leaves")
    first = atlas.projection_matrix("l1", (0.0, 0.0, 0.0), (0.0, 0.0, 1.0),
                                    30.0)
    second = atlas.projection_matrix("l1", (0.0, 0.0, 0.0), (1.0, 0.0, 0.0),
                                     30.0)
    assert first != second


def test_field_of_view_and_position_change_the_matrix():
    atlas = make()
    atlas.register_light("l1", "leaves")
    narrow = atlas.projection_matrix("l1", (0.0, 0.0, 0.0), (0.0, 0.0, 1.0),
                                     20.0)
    wide = atlas.projection_matrix("l1", (0.0, 0.0, 0.0), (0.0, 0.0, 1.0),
                                   60.0)
    moved = atlas.projection_matrix("l1", (5.0, 0.0, 0.0), (0.0, 0.0, 1.0),
                                    20.0)
    assert narrow != wide
    assert narrow != moved


def test_cookie_sampling_never_reads_the_neighbour_cell():
    atlas = make()
    black = [[(0.0, 0.0, 0.0)] * 2] * 2
    atlas.set_cookie("black", black)
    atlas.register_light("white", "leaves")
    atlas.register_light("dark", "black")
    assert atlas.sample("white", (0.99, 0.99))[0] == pytest.approx(1.0)
    assert atlas.sample("dark", (0.99, 0.99))[0] == pytest.approx(0.0)


def test_srgb_is_converted_to_linear_exactly_once():
    atlas = make()
    gray = [[(0.5, 0.5, 0.5)] * 2] * 2
    atlas.set_cookie("gray", gray)
    atlas.register_light("l1", "gray")
    first = atlas.sample("l1", (0.5, 0.5))
    second = atlas.sample("l1", (0.5, 0.5))
    assert first[0] == pytest.approx(0.2140, abs=0.01)
    assert first == second


def test_animation_frames_are_selected_by_time():
    atlas = make()
    atlas.set_animation("leaves", frames=20, fps=10)
    assert atlas.frame_at("leaves", 0.0) == 0
    assert atlas.frame_at("leaves", 105.0) == 1
    assert atlas.frame_at("leaves", 1050.0) == 10


def test_animation_frame_out_of_range_raises():
    atlas = make()
    atlas.set_animation("leaves", frames=3, fps=10)
    with pytest.raises(cookie.CookieAtlasError):
        atlas.frame_at("leaves", 10000.0)


def test_budget_defers_updates_without_changing_slot_mapping():
    atlas = make()
    atlas.register_light("l1", "leaves")
    before = atlas.slot_of("l1")
    atlas.set_budget(0.0001)
    result = atlas.update([{"light_id": "l1", "cookie_id": "leaves"}])
    assert result["deferred"] is True
    assert atlas.slot_of("l1") == before


def test_point_light_cannot_use_a_spot_only_cookie():
    atlas = make()
    atlas.set_cookie("spotmask", COOKIE, spot_only=True)
    with pytest.raises(cookie.CookieAtlasError):
        atlas.register_light("p1", "spotmask", kind="point")


def test_released_light_can_no_longer_be_sampled():
    atlas = make()
    atlas.register_light("l1", "leaves")
    atlas.release_light("l1")
    with pytest.raises(cookie.CookieAtlasError):
        atlas.sample("l1", (0.5, 0.5))


def test_all_registered_lights_keep_distinct_slots_after_updates():
    atlas = make()
    for index in range(4):
        atlas.register_light("l%d" % index, "leaves")
    atlas.update([{"light_id": "l%d" % index, "cookie_id": "leaves"}
                  for index in range(4)])
    slots = atlas.slots()
    assert sorted(slots.values()) == [0, 1, 2, 3]
