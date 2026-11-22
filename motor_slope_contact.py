import math


class ContactSolveError(Exception):
    pass


class SlopeContact:
    STAND_HEIGHT = 1.8
    CROUCH_HEIGHT = 0.9
    MOVE_SPEED = 5.0
    STEP_UP = 1.0
    SNAP_DOWN = 0.5
    JUMP_BUFFER_MS = 100.0
    EPS = 1e-9

    def __init__(self, step_ms=10.0, gravity=10.0, jump_speed=6.0,
                 max_slope_deg=45.0):
        self.step_ms = step_ms
        self.gravity = gravity
        self.jump_speed = jump_speed
        self.max_slope_deg = max_slope_deg
        self._boxes = []
        self._segment = None
        self._normal_override = None
        self._ceiling = None
        self._platform_motion = (0.0, 0.0)
        self._input = (0.0, False)
        self._dash = 0.0
        self._jump_buffer_ms = None
        self._position = (0.0, 0.0)
        self._velocity = (0.0, 0.0)
        self._accumulator_ms = 0.0
        self._steps = 0
        self._grounded = False
        self._ground_source = None
        self._crouched = False
        self._height = self.STAND_HEIGHT

    def add_box(self, x, y, width, height, dynamic=False):
        box = {"x": x, "y": y, "w": width, "h": height, "dynamic": dynamic}
        self._boxes.append(box)
        return box

    def set_ground_segment(self, x0, y0, x1, y1):
        self._segment = (x0, y0, x1, y1)

    def set_ground_normal(self, normal):
        self._normal_override = (normal[0], normal[1])

    def set_ceiling(self, y):
        self._ceiling = y

    def set_platform_motion(self, dx, dy):
        self._platform_motion = (dx, dy)

    def set_input(self, move_x=0.0, jump=False):
        self._input = (move_x, self._input[1] or bool(jump))

    def request_dash(self, dx):
        self._dash = dx

    def set_crouch(self, crouch):
        if crouch:
            self._crouched = True
            self._height = self.CROUCH_HEIGHT
            return True
        if self._ceiling is not None:
            _, y = self._position
            if y + self.STAND_HEIGHT > self._ceiling + self.EPS:
                return False
        self._crouched = False
        self._height = self.STAND_HEIGHT
        return True

    def buffer_jump(self, pressed_at_ms):
        self._jump_buffer_ms = pressed_at_ms

    def consume_jump_buffer(self, landed_at_ms):
        if self._jump_buffer_ms is None:
            return False
        age = landed_at_ms - self._jump_buffer_ms
        self._jump_buffer_ms = None
        return 0.0 <= age <= self.JUMP_BUFFER_MS

    def update(self, frame_ms):
        self._accumulator_ms += frame_ms
        steps = int(self._accumulator_ms / self.step_ms)
        if steps == 0:
            steps = 1
            self._accumulator_ms = 0.0
        else:
            self._accumulator_ms -= steps * self.step_ms
        for _ in range(steps):
            self._step()
            self._steps += 1

    def state(self):
        x, y = self._position
        vx, vy = self._velocity
        return {
            "x": x,
            "y": y,
            "vx": vx,
            "vy": vy,
            "grounded": self._grounded,
            "crouched": self._crouched,
            "height": self._height,
            "steps": self._steps,
            "accumulator_ms": self._accumulator_ms,
        }

    def _step(self):
        dt = self.step_ms / 1000.0
        self._apply_platform_motion()
        move_x, jump = self._input
        if move_x:
            self._move_x(move_x * self.MOVE_SPEED * dt)
        if self._dash:
            self._move_x(self._dash)
            self._dash = 0.0
        if jump and self._grounded:
            self._velocity = (self._velocity[0], self.jump_speed)
            self._grounded = False
            self._ground_source = None
        self._input = (move_x, False)
        self._integrate_vertical(dt)

    def _apply_platform_motion(self):
        dx, dy = self._platform_motion
        if dx == 0.0 and dy == 0.0:
            return
        carried = (
            self._grounded
            and isinstance(self._ground_source, dict)
            and self._ground_source["dynamic"]
        )
        for box in self._boxes:
            if box["dynamic"]:
                box["x"] += dx
                box["y"] += dy
        if carried:
            x, y = self._position
            self._position = (x + dx, y + dy)
        self._platform_motion = (0.0, 0.0)

    def _integrate_vertical(self, dt):
        x, y = self._position
        vx, vy = self._velocity
        if self._grounded:
            support = self._support_at(x, y)
            if support is None or support[0] < y - self.SNAP_DOWN:
                self._grounded = False
                self._ground_source = None
            else:
                self._solve_ground_contact(support[1])
                self._position = (x, support[0])
                self._velocity = (vx, 0.0)
                return
        support = self._support_at(x, y)
        if (vy <= 0.0 and support is not None
                and y <= support[0] + self.EPS):
            self._land(support)
            return
        vy -= self.gravity * dt
        y += vy * dt
        if (vy > 0.0 and self._ceiling is not None
                and y + self._height > self._ceiling):
            y = self._ceiling - self._height
            vy = 0.0
        support = self._support_at(x, y)
        if (vy <= 0.0 and support is not None
                and y <= support[0] + self.EPS):
            self._position = (x, y)
            self._velocity = (vx, vy)
            self._land(support)
            return
        self._position = (x, y)
        self._velocity = (vx, vy)

    def _land(self, support):
        x, _ = self._position
        self._position = (x, support[0])
        self._velocity = (self._velocity[0], 0.0)
        self._grounded = True
        self._ground_source = support[1]
        self._solve_ground_contact(support[1])

    def _support_at(self, x, y):
        best = None
        limit = y + self.STEP_UP + self.EPS
        if self._segment is not None:
            x0, y0, x1, y1 = self._segment
            if min(x0, x1) - self.EPS <= x <= max(x0, x1) + self.EPS:
                if x1 != x0:
                    t = (x - x0) / (x1 - x0)
                    surface_y = y0 + t * (y1 - y0)
                else:
                    surface_y = max(y0, y1)
                if surface_y <= limit:
                    best = (surface_y, "segment")
        for box in self._boxes:
            top = box["y"] + box["h"]
            if (box["x"] - self.EPS <= x <= box["x"] + box["w"] + self.EPS
                    and top <= limit):
                if best is None or top > best[0]:
                    best = (top, box)
        return best

    def _solve_ground_contact(self, source):
        if self._normal_override is not None:
            nx, ny = self._normal_override
        elif source == "segment":
            x0, y0, x1, y1 = self._segment
            nx, ny = -(y1 - y0), x1 - x0
            if ny < 0.0:
                nx, ny = -nx, -ny
        else:
            nx, ny = 0.0, 1.0
        length = math.hypot(nx, ny)
        if length < self.EPS:
            raise ContactSolveError("degenerate ground normal")
        nx, ny = nx / length, ny / length
        if ny <= 0.0:
            raise ContactSolveError("ground normal points into the ground")
        slope_deg = math.degrees(math.atan2(abs(nx), ny))
        if slope_deg > self.max_slope_deg:
            raise ContactSolveError(
                "slope %.2fdeg exceeds limit %.2fdeg"
                % (slope_deg, self.max_slope_deg))

    def _move_x(self, dx):
        if dx == 0.0:
            return
        x, y = self._position
        target = x + dx
        head = y + self._height
        for box in self._boxes:
            top = box["y"] + box["h"]
            if top <= y + self.STEP_UP + self.EPS:
                continue
            if y >= top - self.EPS or head <= box["y"] + self.EPS:
                continue
            left = box["x"]
            right = box["x"] + box["w"]
            if dx > 0.0 and x <= left + self.EPS and target > left:
                target = left
            elif dx < 0.0 and x >= right - self.EPS and target < right:
                target = right
        self._position = (target, y)
