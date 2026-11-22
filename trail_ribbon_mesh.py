class TrailError(Exception):
    pass


class TrailRibbon:
    def __init__(self, max_segments=64, lifetime_ms=300, max_segment_length=10.0,
                 time_base_ms=10.0):
        self.max_segments = max_segments
        self.lifetime_ms = lifetime_ms
        self.max_segment_length = max_segment_length
        self.time_base_ms = time_base_ms
        self._samples = []
        self._paused = False
        self._allocations = 0
        self._vertex_count = 0
        self._color = (1.0, 1.0, 1.0, 1.0)
        self._matrix = ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0))

    def add_sample(self, timestamp_ms, bone_matrix, position):
        raise NotImplementedError()

    def build(self, now_ms):
        raise NotImplementedError()

    def set_paused(self, paused):
        raise NotImplementedError()

    def release(self):
        raise NotImplementedError()

    def set_material(self, color):
        raise NotImplementedError()

    def material(self):
        raise NotImplementedError()

    def pool_stats(self):
        raise NotImplementedError()

    def sort_indices(self, camera_position):
        raise NotImplementedError()

    def samples(self):
        raise NotImplementedError()
