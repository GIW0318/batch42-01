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

    def add_team(self, team_id):
        raise NotImplementedError()

    def add_unit(self, unit_id, team_id, x, y, sight, alive=True,
                 invisible=False):
        raise NotImplementedError()

    def remove_unit(self, unit_id):
        raise NotImplementedError()

    def add_blocker(self, x, y):
        raise NotImplementedError()

    def update(self, frame):
        raise NotImplementedError()

    def visible(self, team_id, version=None, viewer=None):
        raise NotImplementedError()

    def explored(self, team_id):
        raise NotImplementedError()

    def visible_units(self, team_id):
        raise NotImplementedError()

    def set_budget(self, cells):
        raise NotImplementedError()

    def snapshot(self):
        raise NotImplementedError()

    def restore(self, snapshot):
        raise NotImplementedError()
