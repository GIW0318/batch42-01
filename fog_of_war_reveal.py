class VisionDeniedError(Exception):
    pass


class FogReveal:
    def __init__(self, width, height, max_cells_per_update=None):
        self.width = width
        self.height = height
        self.max_cells_per_update = max_cells_per_update
        self._teams = {}
        self._units = {}
        self._blockers = set()
        self._version = 0
        self._visible = {}
        self._explored = {}
        self._last_frame = None
        self._last_result = {}

    def add_team(self, team_id):
        if team_id not in self._teams:
            self._teams[team_id] = {}
            self._explored[team_id] = bytearray(self.width * self.height)
            self._visible[team_id] = bytearray(self.width * self.height)
            self._last_result[team_id] = frozenset()

    def add_unit(self, unit_id, team_id, x, y, sight, alive=True,
                 invisible=False):
        self._authorize(team_id)
        self._units[unit_id] = {
            "unit_id": unit_id,
            "team_id": team_id,
            "x": x,
            "y": y,
            "sight": sight,
            "alive": alive,
            "invisible": invisible,
        }

    def remove_unit(self, unit_id):
        self._units.pop(unit_id, None)

    def add_blocker(self, x, y):
        if 0 <= x < self.width and 0 <= y < self.height:
            self._blockers.add((x, y))

    def update(self, frame):
        if frame == self._last_frame:
            return dict(self._last_result)

        self._version += 1
        self._last_frame = frame

        result = {}
        for team_id in sorted(self._teams):
            target = self._target_cells(team_id)
            explored = self._explored[team_id]
            pending = [cell for cell in sorted(target)
                       if not explored[self._index(*cell)]]
            budget = self.max_cells_per_update
            if budget is not None:
                pending = pending[:budget]
            for cell in pending:
                explored[self._index(*cell)] = 1

            visible = bytearray(self.width * self.height)
            visible_cells = set()
            for cell in target:
                if explored[self._index(*cell)]:
                    visible[self._index(*cell)] = 1
                    visible_cells.add(cell)

            snapshot = frozenset(visible_cells)
            self._visible[team_id] = visible
            self._last_result[team_id] = snapshot
            result[team_id] = snapshot

        return result

    def visible(self, team_id, version=None, viewer=None):
        self._authorize(team_id, viewer)
        if version is not None and version != self._version:
            return None
        return self._bitmap_cells(self._visible[team_id])

    def explored(self, team_id):
        self._authorize(team_id)
        return self._bitmap_cells(self._explored[team_id])

    def visible_units(self, team_id):
        self._authorize(team_id)
        visible = self._visible[team_id]
        seen = set()
        for unit_id in sorted(self._units):
            unit = self._units[unit_id]
            if not unit["alive"]:
                continue
            if unit["team_id"] == team_id:
                seen.add(unit_id)
            elif not unit["invisible"]:
                cell_index = self._index(unit["x"], unit["y"])
                if cell_index is not None and visible[cell_index]:
                    seen.add(unit_id)
        return seen

    def set_budget(self, cells):
        self.max_cells_per_update = cells

    def snapshot(self):
        return {
            "width": self.width,
            "height": self.height,
            "max_cells_per_update": self.max_cells_per_update,
            "version": self._version,
            "last_frame": self._last_frame,
            "teams": list(self._teams),
            "units": [dict(unit) for unit_id, unit in
                      sorted(self._units.items())],
            "blockers": sorted(self._blockers),
            "explored": {
                team_id: bytes(self._explored[team_id])
                for team_id in self._teams
            },
        }

    def restore(self, snapshot):
        self.width = snapshot["width"]
        self.height = snapshot["height"]
        self.max_cells_per_update = snapshot["max_cells_per_update"]
        self._version = snapshot["version"]
        self._last_frame = snapshot["last_frame"]

        self._teams = {}
        self._explored = {}
        self._visible = {}
        self._last_result = {}
        for team_id in snapshot["teams"]:
            self.add_team(team_id)
            bitmap = bytearray(self.width * self.height)
            saved = snapshot["explored"][team_id]
            bitmap[:len(saved)] = saved
            self._explored[team_id] = bitmap

        self._units = {}
        for unit in snapshot["units"]:
            self.add_unit(
                unit["unit_id"], unit["team_id"], unit["x"], unit["y"],
                unit["sight"], alive=unit["alive"],
                invisible=unit["invisible"],
            )

        self._blockers = set(tuple(cell) for cell in snapshot["blockers"])

        for team_id in self._teams:
            target = self._target_cells(team_id)
            explored = self._explored[team_id]
            for cell_index, flag in enumerate(explored):
                if flag:
                    cell = (cell_index % self.width,
                            cell_index // self.width)
                    if cell not in target:
                        explored[cell_index] = 0
            visible = bytearray(self.width * self.height)
            visible_cells = set()
            for cell in target:
                if explored[self._index(*cell)]:
                    visible[self._index(*cell)] = 1
                    visible_cells.add(cell)
            self._visible[team_id] = visible
            self._last_result[team_id] = frozenset(visible_cells)

    def _index(self, x, y):
        if 0 <= x < self.width and 0 <= y < self.height:
            return y * self.width + x
        return None

    def _authorize(self, team_id, viewer=None):
        if team_id not in self._teams:
            raise VisionDeniedError(
                "team %r has no vision channel" % (team_id,))
        if viewer is not None and viewer != team_id:
            raise VisionDeniedError(
                "viewer %r cannot read team %r vision" % (viewer, team_id))

    def _bitmap_cells(self, bitmap):
        cells = set()
        for index, flag in enumerate(bitmap):
            if flag:
                cells.add((index % self.width, index // self.width))
        return cells

    def _target_cells(self, team_id):
        target = set()
        for unit_id in sorted(self._units):
            unit = self._units[unit_id]
            if unit["team_id"] != team_id or not unit["alive"]:
                continue
            target.update(self._unit_cells(unit))
        return target

    def _unit_cells(self, unit):
        origin_x, origin_y = unit["x"], unit["y"]
        sight = unit["sight"]
        cells = set()
        for dy in range(-sight, sight + 1):
            for dx in range(-sight, sight + 1):
                if abs(dx) > sight or abs(dy) > sight:
                    continue
                target_x, target_y = origin_x + dx, origin_y + dy
                if not (0 <= target_x < self.width and
                        0 <= target_y < self.height):
                    continue
                if self._los(origin_x, origin_y, target_x, target_y):
                    cells.add((target_x, target_y))
        return cells

    def _los(self, origin_x, origin_y, target_x, target_y):
        delta_x = target_x - origin_x
        delta_y = target_y - origin_y
        distance = max(abs(delta_x), abs(delta_y))
        if distance == 0:
            return True
        previous = (origin_x, origin_y)
        for step in range(1, distance + 1):
            point_x = origin_x + delta_x * step / distance
            point_y = origin_y + delta_y * step / distance
            for offset in (-0.5, 0.0, 0.5):
                cell = (int(round(point_x + offset)),
                        int(round(point_y + offset)))
                if cell == previous or cell == (target_x, target_y):
                    continue
                if cell in self._blockers:
                    return False
            previous = cell
        return True
