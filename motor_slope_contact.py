class ContactSolveError(Exception):
    pass


class SlopeContact:
    def __init__(self, step_ms=10.0, gravity=10.0, jump_speed=6.0,
                 max_slope_deg=45.0):
        self.step_ms = step_ms
        self.gravity = gravity
        self.jump_speed = jump_speed
        self.max_slope_deg = max_slope_deg
        self._bodies = []
        self._ground = None
        self._ground_normal = (0.0, 1.0)
        self._ceiling = None
        self._platform_motion = (0.0, 0.0)
        self._input = (0.0, False)
        self._position = (0.0, 0.0)
        self._velocity = (0.0, 0.0)
        self._accumulator_ms = 0.0
        self._steps = 0
        self._grounded = False
        self._crouched = False
        self._height = 1.8

    def add_box(self, x, y, width, height, dynamic=False):
        raise NotImplementedError()

    def set_ground_segment(self, x0, y0, x1, y1):
        raise NotImplementedError()

    def set_ground_normal(self, normal):
        raise NotImplementedError()

    def set_ceiling(self, y):
        raise NotImplementedError()

    def set_platform_motion(self, dx, dy):
        raise NotImplementedError()

    def set_input(self, move_x=0.0, jump=False):
        raise NotImplementedError()

    def request_dash(self, dx):
        raise NotImplementedError()

    def set_crouch(self, crouch):
        raise NotImplementedError()

    def buffer_jump(self, pressed_at_ms):
        raise NotImplementedError()

    def consume_jump_buffer(self, landed_at_ms):
        raise NotImplementedError()

    def update(self, frame_ms):
        raise NotImplementedError()

    def state(self):
        raise NotImplementedError()
