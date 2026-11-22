class NavMeshQueryError(Exception):
    def __init__(self, message, cell=None):
        super().__init__(message)
        self.cell = cell


class CorridorFollow:
    def __init__(self, cell_size=1.0, max_replans_per_frame=8):
        self.cell_size = cell_size
        self.max_replans_per_frame = max_replans_per_frame
        self.version = 0
        self._polygons = {}
        self._links = {}
        self._agents = {}
        self._queue = []

    def load_navmesh(self, version, polygons, links=None):
        raise NotImplementedError()

    def set_walkable(self, polygon_id, walkable):
        raise NotImplementedError()

    def polygon_at(self, point):
        raise NotImplementedError()

    def add_link(self, link_id, from_polygon, to_polygon, one_way=True):
        raise NotImplementedError()

    def find_path(self, start, goal):
        raise NotImplementedError()

    def follow(self, agent_id, position, path=None):
        raise NotImplementedError()

    def apply_avoidance(self, agent_id, position, offset):
        raise NotImplementedError()

    def request_replan(self, agent_id, goal):
        raise NotImplementedError()

    def process_replans(self):
        raise NotImplementedError()

    def begin_rebuild(self, next_version):
        raise NotImplementedError()

    def commit_rebuild(self, polygons):
        raise NotImplementedError()

    def save_state(self):
        raise NotImplementedError()

    def load_state(self, state):
        raise NotImplementedError()
