import copy


_VALID_TYPES = ("move", "attack", "build")
_FORMATION_COLUMNS = 4
_BACK_ROW_TYPES = ("tank", "artillery")


class CommandDispatch:
    def __init__(self, max_queue=8, frame_budget=50):
        self.max_queue = max_queue
        self.frame_budget = frame_budget
        self._units = {}
        self._queues = {}
        self._current = {}
        self._seqs = {}
        self._rally = None
        self._resources = {}
        self._order = 0
        self._spawn_counter = 0

    def add_unit(self, unit_id, player_id, unit_type="rifle", alive=True):
        self._units[unit_id] = {
            "player_id": player_id,
            "unit_type": unit_type,
            "alive": alive,
        }
        self._queues.setdefault(unit_id, [])
        return unit_id

    def kill_unit(self, unit_id):
        unit = self._units.get(unit_id)
        if unit is None:
            return False
        unit["alive"] = False
        self._queues[unit_id] = []
        self._current.pop(unit_id, None)
        return True

    def set_resources(self, player_id, amount):
        self._resources[player_id] = amount

    def _validate(self, player_id, command):
        if not isinstance(command, dict):
            return "INVALID_COMMAND"
        command_type = command.get("type")
        if command_type not in _VALID_TYPES:
            return "INVALID_COMMAND"
        target = command.get("target")
        if target is None:
            return "INVALID_COMMAND"
        if command_type == "attack":
            target_unit = self._units.get(target)
            if target_unit is None or not target_unit["alive"]:
                return "INVALID_TARGET"
        if command_type == "build":
            cost = command.get("cost", 0)
            if self._resources.get(player_id, 0) < cost:
                return "INSUFFICIENT_RESOURCES"
        return None

    def _make_entry(self, command, client_seq, frame):
        entry = dict(command)
        entry["client_seq"] = client_seq
        entry["frame"] = frame
        entry["order"] = self._order
        self._order += 1
        return entry

    def issue(self, player_id, unit_id, command, client_seq, frame=0):
        unit = self._units.get(unit_id)
        if unit is None or not unit["alive"]:
            return {"code": "DEAD_UNIT"}
        seen = self._seqs.setdefault((player_id, unit_id), set())
        if client_seq in seen:
            return {"code": "OK", "duplicate": True}
        error = self._validate(player_id, command)
        if error is not None:
            return {"code": error}
        queue = self._queues.setdefault(unit_id, [])
        if len(queue) >= self.max_queue:
            return {"code": "QUEUE_FULL"}
        queue.append(self._make_entry(command, client_seq, frame))
        seen.add(client_seq)
        if command.get("type") == "build":
            cost = command.get("cost", 0)
            self._resources[player_id] = self._resources.get(player_id, 0) - cost
        return {"code": "OK"}

    def stop(self, unit_id):
        self._queues[unit_id] = []
        self._current.pop(unit_id, None)

    def cancel_last(self, unit_id):
        queue = self._queues.get(unit_id)
        if not queue:
            return False
        queue.pop()
        return True

    def replace_queue(self, unit_id, commands):
        unit = self._units.get(unit_id)
        if unit is None or not unit["alive"]:
            return {"code": "DEAD_UNIT"}
        if len(commands) > self.max_queue:
            return {"code": "QUEUE_FULL"}
        player_id = unit["player_id"]
        entries = []
        for command in commands:
            error = self._validate(player_id, command)
            if error is not None:
                return {"code": error}
            entries.append(self._make_entry(command, None, 0))
        self._queues[unit_id] = entries
        return {"code": "OK"}

    def process_frame(self, frame):
        candidates = []
        for unit_id, queue in self._queues.items():
            unit = self._units.get(unit_id)
            if unit is None or not unit["alive"] or not queue:
                continue
            head = queue[0]
            if head.get("frame", 0) > frame:
                continue
            player_id = unit["player_id"]
            candidates.append((
                "" if player_id is None else str(player_id),
                str(unit_id),
                head.get("frame", 0),
                head.get("order", 0),
                unit_id,
            ))
        candidates.sort()
        executed = []
        for _, _, _, _, unit_id in candidates[: self.frame_budget]:
            queue = self._queues.get(unit_id)
            if not queue:
                continue
            entry = queue.pop(0)
            self._current[unit_id] = entry
            unit = self._units[unit_id]
            executed.append({
                "unit_id": unit_id,
                "player_id": unit["player_id"],
                "command": {k: v for k, v in entry.items() if k != "order"},
            })
        return executed

    def queue(self, unit_id):
        return [dict(entry) for entry in self._queues.get(unit_id, [])]

    def set_rally(self, point):
        self._rally = point

    def spawn(self, unit_type, unit_id=None):
        if unit_id is None:
            self._spawn_counter += 1
            unit_id = "spawn_%d" % self._spawn_counter
        self.add_unit(unit_id, None, unit_type=unit_type)
        if self._rally is not None:
            entry = self._make_entry(
                {"type": "move", "target": self._rally},
                client_seq=self._order,
                frame=0,
            )
            self._queues[unit_id].append(entry)
        return unit_id

    def select(self, player_id, unit_ids, visible):
        selected = []
        for unit_id in unit_ids:
            unit = self._units.get(unit_id)
            if unit is None or not unit["alive"]:
                continue
            if unit["player_id"] != player_id:
                continue
            if unit_id not in visible:
                continue
            selected.append(unit_id)
        return selected

    def form_up(self, unit_ids, anchor):
        def row_rank(unit_id):
            unit = self._units.get(unit_id)
            unit_type = unit["unit_type"] if unit else "rifle"
            return 1 if unit_type in _BACK_ROW_TYPES else 0

        ordered = sorted(unit_ids, key=row_rank)
        positions = {}
        for index, unit_id in enumerate(ordered):
            row = index // _FORMATION_COLUMNS
            column = index % _FORMATION_COLUMNS
            positions[unit_id] = (anchor[0] + column - 1, anchor[1] - row)
        return positions

    def snapshot(self):
        return copy.deepcopy({
            "max_queue": self.max_queue,
            "frame_budget": self.frame_budget,
            "units": self._units,
            "queues": self._queues,
            "current": self._current,
            "seqs": {key: sorted(value) for key, value in self._seqs.items()},
            "rally": self._rally,
            "resources": self._resources,
            "order": self._order,
            "spawn_counter": self._spawn_counter,
        })

    def restore(self, snapshot):
        data = copy.deepcopy(snapshot)
        self.max_queue = data["max_queue"]
        self.frame_budget = data["frame_budget"]
        self._units = data["units"]
        self._queues = data["queues"]
        self._current = data["current"]
        self._seqs = {key: set(value) for key, value in data["seqs"].items()}
        self._rally = data["rally"]
        self._resources = data["resources"]
        self._order = data["order"]
        self._spawn_counter = data["spawn_counter"]
