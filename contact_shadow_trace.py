class ContactShadowError(Exception):
    pass


class ContactShadowTrace:
    def __init__(self, max_steps=16, step_px=2.0, max_distance=100.0,
                 thickness=1.0):
        self.max_steps = max_steps
        self.step_px = step_px
        self.max_distance = max_distance
        self.thickness = thickness
        self._budget = None
        self._jitter_seed = 7
        self._last_stats = {}

    def trace(self, uv, depth_buffer, normal_buffer, light_dir, camera_dir,
              height=0.0, mask=1.0):
        raise NotImplementedError()

    def sample_occlusion(self, distance, thickness, angle_deg, height=0.0):
        raise NotImplementedError()

    def set_budget(self, max_steps_per_frame):
        raise NotImplementedError()

    def jitter(self, frame):
        raise NotImplementedError()

    def combine(self, contact, cascaded):
        raise NotImplementedError()

    def params(self, eye="left"):
        raise NotImplementedError()

    def last_stats(self):
        raise NotImplementedError()
