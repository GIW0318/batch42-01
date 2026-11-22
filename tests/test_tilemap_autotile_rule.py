import pytest

import tilemap_autotile_rule
from tilemap_autotile_rule import (
    TilemapAutotileRule,
    TilemapError,
    UnsupportedTileError,
)


def _empty(width=8, height=8):
    return {"width": width, "height": height, "tiles": {},
            "tileset_version": 3}


def test_autotile_updates_both_sides_of_block_boundary():
    game = TilemapAutotileRule(block_size=4)
    game.load(_empty())
    game.set_tile(3, 3, "grass")
    game.set_tile(4, 3, "grass")
    result = game.autotile(3, 3)
    assert "3,3" in result["updated"]
    assert "4,3" in result["updated"]
    assert game.tile_at(4, 3) == result["updated"]["4,3"]


def test_diagonal_neighbor_participates_in_corner_rule():
    game = TilemapAutotileRule(block_size=4)
    game.load(_empty())
    game.set_tile(2, 2, "grass")
    game.set_tile(3, 3, "grass")
    result = game.autotile(2, 2)
    assert "SE" in result["updated"]["2,2"]


def test_undo_reverts_a_whole_stroke():
    game = TilemapAutotileRule()
    game.load(_empty())
    game.fill(0, 0, 2, 2, "grass")
    assert game.tile_at(1, 1) == "grass"
    assert game.undo() is True
    assert game.tile_at(0, 0) is None
    assert game.tile_at(1, 1) is None
    assert game.tile_at(2, 2) is None


def test_undo_stack_honors_limit():
    game = TilemapAutotileRule(undo_limit=3)
    game.load(_empty(10, 4))
    for index in range(5):
        game.fill(index, 0, index, 0, "grass")
    for _ in range(3):
        assert game.undo() is True
    assert game.tile_at(0, 0) == "grass"
    assert game.tile_at(1, 0) == "grass"
    assert game.tile_at(2, 0) is None
    assert game.undo() is False


def test_redo_reproduces_undone_map_exactly():
    game = TilemapAutotileRule()
    game.load(_empty())
    game.fill(1, 1, 3, 2, "sand")
    before = game.export_map()
    game.undo()
    assert game.redo() is True
    assert game.export_map() == before


def test_import_unknown_tile_rejects_whole_map():
    game = TilemapAutotileRule()
    game.load(_empty(4, 4))
    doc = _empty(4, 4)
    doc["tiles"] = {"0,0": "grass", "1,1": "mystery"}
    with pytest.raises(UnsupportedTileError) as exc:
        game.import_map(doc)
    assert exc.value.tile_ids == ["mystery"]
    assert game.export_map()["tiles"] == {}


def test_set_unknown_tile_is_rejected_without_editing():
    game = TilemapAutotileRule()
    game.load(_empty())
    with pytest.raises(UnsupportedTileError) as exc:
        game.set_tile(0, 0, "mystery")
    assert exc.value.code == "unsupported_tile"
    assert game.tile_at(0, 0) is None


def test_stale_transaction_cannot_overwrite_newer_commit():
    game = TilemapAutotileRule()
    game.load(_empty())
    older = game.begin_transaction()
    newer = game.begin_transaction()
    game.set_tile(0, 0, "grass", transaction=older)
    game.set_tile(0, 0, "water", transaction=newer)
    game.commit_transaction(newer)
    with pytest.raises(TilemapError) as exc:
        game.commit_transaction(older)
    assert exc.value.code == "stale_transaction"
    assert game.tile_at(0, 0) == "water"


def test_collision_layer_matches_visual_water_tile():
    game = TilemapAutotileRule()
    game.load(_empty())
    game.set_tile(1, 1, "water")
    assert game.tile_at(1, 1) == "water"
    assert game.collision_at(1, 1) is True


def test_palette_replacement_updates_collision_and_export():
    game = TilemapAutotileRule()
    game.load(_empty())
    game.set_tile(1, 1, "grass")
    game.replace_palette("grass", "water")
    assert game.tile_at(1, 1) == "water"
    assert game.collision_at(1, 1) is True
    assert game.export_map()["tiles"]["1,1"] == "water"


def test_save_and_load_keep_tileset_version():
    game = TilemapAutotileRule(tileset_version=3)
    game.load(_empty())
    game.set_tile(2, 2, "stone")
    blob = game.save()
    assert blob["tileset_version"] == 3
    restored = TilemapAutotileRule()
    restored.load_saved(blob)
    assert restored.tile_at(2, 2) == "stone"


def test_unsupported_saved_version_keeps_original_map():
    game = TilemapAutotileRule()
    game.load(_empty())
    game.set_tile(0, 0, "grass")
    before = game.export_map()
    bad = _empty(4, 4)
    bad["tileset_version"] = 99
    with pytest.raises(TilemapError) as exc:
        game.load_saved(bad)
    assert exc.value.code == "unsupported_version"
    assert game.export_map() == before


def test_block_index_uses_tile_size_pixels():
    game = TilemapAutotileRule(tile_size=24, block_size=32)
    assert game.block_of(0) == 0
    assert game.block_of(1) == 0
    assert game.block_of(2) == 1


def test_render_order_sorts_by_y_then_layer():
    game = TilemapAutotileRule()
    game.load(_empty())
    game.set_tile(0, 2, "grass", layer=0)
    game.set_tile(0, 1, "stone", layer=1)
    game.set_tile(0, 1, "grass", layer=0)
    assert game.render_order() == [(0, 1, 0), (0, 1, 1), (0, 2, 0)]


def test_autotile_touches_only_local_neighborhood():
    game = TilemapAutotileRule()
    game.load(_empty(20, 20))
    for x in range(20):
        for y in range(20):
            game.set_tile(x, y, "grass")
    result = game.autotile(10, 10)
    assert len(result["updated"]) <= 9
    assert len(game.last_touched()) <= 9
