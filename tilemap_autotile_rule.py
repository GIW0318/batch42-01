"""Tilemap autotile neighbor rules, undo and versioned import (skeleton).

Autotiling uses four and eight neighbors across block edges, edits undo per
stroke, unknown tiles reject the whole import and saved maps carry their
tileset version.  No behaviour is implemented yet.
"""

UNSUPPORTED_TILE = "unsupported_tile"
UNSUPPORTED_VERSION = "unsupported_version"
STALE_TRANSACTION = "stale_transaction"
OUT_OF_BOUNDS = "out_of_bounds"


class TilemapError(Exception):
    def __init__(self, code, message=""):
        self.code = code
        super().__init__(code if not message else "%s: %s" % (code, message))


class UnsupportedTileError(TilemapError):
    def __init__(self, tile_ids, code=UNSUPPORTED_TILE, message=""):
        self.tile_ids = list(tile_ids)
        super().__init__(code, message)


class TilemapAutotileRule:
    def __init__(self, tile_size=24, block_size=32, undo_limit=100,
                 tileset_version=3):
        self.tile_size = tile_size
        self.block_size = block_size
        self.undo_limit = undo_limit
        self.tileset_version = tileset_version
        self.known_tiles = {"grass", "water", "stone", "sand"}

    def load(self, doc):
        raise NotImplementedError

    def set_tile(self, x, y, tile_id, layer=0, transaction=None):
        raise NotImplementedError

    def tile_at(self, x, y, layer=0):
        raise NotImplementedError

    def autotile(self, x, y, layer=0):
        raise NotImplementedError

    def fill(self, x0, y0, x1, y1, tile_id, layer=0):
        raise NotImplementedError

    def undo(self):
        raise NotImplementedError

    def redo(self):
        raise NotImplementedError

    def import_map(self, doc):
        raise NotImplementedError

    def export_map(self):
        raise NotImplementedError

    def save(self):
        raise NotImplementedError

    def load_saved(self, blob):
        raise NotImplementedError

    def migrate(self, doc):
        raise NotImplementedError

    def collision_at(self, x, y):
        raise NotImplementedError

    def replace_palette(self, old, new):
        raise NotImplementedError

    def begin_transaction(self):
        raise NotImplementedError

    def commit_transaction(self, tx):
        raise NotImplementedError

    def abort_transaction(self, tx):
        raise NotImplementedError

    def last_touched(self):
        raise NotImplementedError

    def block_of(self, coord):
        raise NotImplementedError

    def render_order(self):
        raise NotImplementedError
