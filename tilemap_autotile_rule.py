"""Tilemap autotile neighbor rules, undo and versioned import.

Autotiling uses four and eight neighbors across block edges, edits undo per
stroke, unknown tiles reject the whole import and saved maps carry their
tileset version.
"""

UNSUPPORTED_TILE = "unsupported_tile"
UNSUPPORTED_VERSION = "unsupported_version"
STALE_TRANSACTION = "stale_transaction"
OUT_OF_BOUNDS = "out_of_bounds"

DIRECTIONS = ("N", "NE", "E", "SE", "S", "SW", "W", "NW")
OFFSETS = {
    "N": (0, -1), "NE": (1, -1), "E": (1, 0), "SE": (1, 1),
    "S": (0, 1), "SW": (-1, 1), "W": (-1, 0), "NW": (-1, -1),
}
SOLID_TILES = frozenset({"water", "stone"})

LEGACY_TILE_IDS = {
    1: {"gras": "grass", "watr": "water", "ston": "stone"},
    2: {"dirt": "sand"},
}


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
        self.width = 0
        self.height = 0
        self._tiles = {}
        self._undo = []
        self._redo = []
        self._counter = 0
        self._cell_version = {}
        self._tx_seq = 0
        self._touched = []

    @staticmethod
    def _base(tile):
        return tile.split("[", 1)[0]

    @staticmethod
    def _key(x, y, layer=0):
        if layer:
            return "%d,%d@%d" % (x, y, layer)
        return "%d,%d" % (x, y)

    @staticmethod
    def _parse_key(key):
        head, _, layer = key.partition("@")
        x, y = head.split(",")
        return int(x), int(y), int(layer) if layer else 0

    def _check_tile(self, tile_id):
        base = self._base(tile_id)
        if base not in self.known_tiles:
            raise UnsupportedTileError([base])

    def _check_bounds(self, x, y):
        if not (0 <= x < self.width and 0 <= y < self.height):
            raise TilemapError(OUT_OF_BOUNDS, "(%d, %d)" % (x, y))

    def _validated_doc(self, doc):
        version = doc.get("tileset_version", self.tileset_version)
        if version != self.tileset_version:
            doc = self.migrate(doc)
        width = doc["width"]
        height = doc["height"]
        tiles = {}
        unknown = set()
        for key, tile in doc.get("tiles", {}).items():
            base = self._base(tile)
            if base not in self.known_tiles:
                unknown.add(base)
                continue
            tiles[self._parse_key(key)] = tile
        if unknown:
            raise UnsupportedTileError(sorted(unknown))
        return width, height, tiles

    def _install(self, width, height, tiles):
        self.width = width
        self.height = height
        self._tiles = dict(tiles)
        self._undo = []
        self._redo = []
        self._cell_version = {}
        self._counter += 1
        self._touched = []

    def _push_undo(self, entry):
        self._undo.append(entry)
        if len(self._undo) > self.undo_limit:
            del self._undo[:len(self._undo) - self.undo_limit]
        self._redo = []

    def _apply_cells(self, writes):
        entry = {}
        for cell, tile in writes.items():
            old = self._tiles.get(cell)
            if old != tile:
                entry[cell] = (old, tile)
        if not entry:
            return
        for cell, (old, tile) in entry.items():
            if tile is None:
                self._tiles.pop(cell, None)
            else:
                self._tiles[cell] = tile
        self._push_undo(entry)
        self._counter += 1
        for cell in entry:
            self._cell_version[cell] = self._counter
        self._touched = [self._key(*cell) for cell in sorted(entry)]

    def load(self, doc):
        width, height, tiles = self._validated_doc(doc)
        self._install(width, height, tiles)

    def set_tile(self, x, y, tile_id, layer=0, transaction=None):
        self._check_tile(tile_id)
        self._check_bounds(x, y)
        cell = (x, y, layer)
        if transaction is not None:
            if not transaction.get("active"):
                raise TilemapError(STALE_TRANSACTION, "transaction is closed")
            transaction["writes"][cell] = tile_id
            return
        self._apply_cells({cell: tile_id})

    def tile_at(self, x, y, layer=0):
        return self._tiles.get((x, y, layer))

    def _variant(self, x, y, layer):
        base = self._base(self._tiles[(x, y, layer)])
        matched = []
        for name in DIRECTIONS:
            dx, dy = OFFSETS[name]
            neighbor = self._tiles.get((x + dx, y + dy, layer))
            if neighbor is not None and self._base(neighbor) == base:
                matched.append(name)
        if not matched:
            return base
        return "%s[%s]" % (base, ",".join(matched))

    def autotile(self, x, y, layer=0):
        updated = {}
        writes = {}
        for dy in (-1, 0, 1):
            for dx in (-1, 0, 1):
                cell = (x + dx, y + dy, layer)
                current = self._tiles.get(cell)
                if current is None:
                    continue
                variant = self._variant(*cell)
                if variant != current:
                    writes[cell] = variant
                    updated[self._key(*cell)] = variant
        self._apply_cells(writes)
        return {"updated": updated}

    def fill(self, x0, y0, x1, y1, tile_id, layer=0):
        self._check_tile(tile_id)
        xa, xb = sorted((x0, x1))
        ya, yb = sorted((y0, y1))
        xa = max(xa, 0)
        ya = max(ya, 0)
        xb = min(xb, self.width - 1)
        yb = min(yb, self.height - 1)
        writes = {}
        for x in range(xa, xb + 1):
            for y in range(ya, yb + 1):
                writes[(x, y, layer)] = tile_id
        self._apply_cells(writes)

    def undo(self):
        if not self._undo:
            return False
        entry = self._undo.pop()
        for cell, (old, new) in entry.items():
            if old is None:
                self._tiles.pop(cell, None)
            else:
                self._tiles[cell] = old
        self._redo.append(entry)
        self._counter += 1
        for cell in entry:
            self._cell_version[cell] = self._counter
        self._touched = [self._key(*cell) for cell in sorted(entry)]
        return True

    def redo(self):
        if not self._redo:
            return False
        entry = self._redo.pop()
        for cell, (old, new) in entry.items():
            if new is None:
                self._tiles.pop(cell, None)
            else:
                self._tiles[cell] = new
        self._undo.append(entry)
        self._counter += 1
        for cell in entry:
            self._cell_version[cell] = self._counter
        self._touched = [self._key(*cell) for cell in sorted(entry)]
        return True

    def import_map(self, doc):
        width, height, tiles = self._validated_doc(doc)
        self._install(width, height, tiles)

    def export_map(self):
        tiles = {}
        for cell in sorted(self._tiles):
            tiles[self._key(*cell)] = self._tiles[cell]
        return {"width": self.width, "height": self.height,
                "tiles": tiles, "tileset_version": self.tileset_version}

    def save(self):
        return self.export_map()

    def load_saved(self, blob):
        width, height, tiles = self._validated_doc(blob)
        self._install(width, height, tiles)

    def migrate(self, doc):
        version = doc.get("tileset_version")
        if not isinstance(version, int) or isinstance(version, bool) \
                or version < 1 or version > self.tileset_version:
            raise TilemapError(UNSUPPORTED_VERSION,
                               "tileset version %r" % (version,))
        migrated = {"width": doc["width"], "height": doc["height"],
                    "tiles": dict(doc.get("tiles", {})),
                    "tileset_version": version}
        while migrated["tileset_version"] < self.tileset_version:
            remap = LEGACY_TILE_IDS.get(migrated["tileset_version"], {})
            tiles = {}
            for key, tile in migrated["tiles"].items():
                base = self._base(tile)
                tiles[key] = remap.get(base, base) + tile[len(base):]
            migrated["tiles"] = tiles
            migrated["tileset_version"] += 1
        return migrated

    def collision_at(self, x, y):
        for (cx, cy, _layer), tile in self._tiles.items():
            if cx == x and cy == y and self._base(tile) in SOLID_TILES:
                return True
        return False

    def replace_palette(self, old, new):
        self._check_tile(new)
        writes = {}
        for cell, tile in self._tiles.items():
            base = self._base(tile)
            if base == old:
                writes[cell] = new + tile[len(base):]
        self._apply_cells(writes)

    def begin_transaction(self):
        self._tx_seq += 1
        return {"id": self._tx_seq, "begin": self._counter,
                "writes": {}, "active": True}

    def commit_transaction(self, tx):
        if not tx.get("active"):
            raise TilemapError(STALE_TRANSACTION, "transaction is closed")
        for cell in tx["writes"]:
            if self._cell_version.get(cell, 0) > tx["begin"]:
                raise TilemapError(
                    STALE_TRANSACTION,
                    "cell %s changed after transaction %d began"
                    % (self._key(*cell), tx["id"]))
        tx["active"] = False
        self._apply_cells(dict(tx["writes"]))

    def abort_transaction(self, tx):
        tx["active"] = False
        tx["writes"] = {}

    def last_touched(self):
        return list(self._touched)

    def block_of(self, coord):
        return coord * self.tile_size // self.block_size

    def render_order(self):
        return sorted(self._tiles, key=lambda c: (c[1], c[2], c[0]))
