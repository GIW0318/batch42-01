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

    def add_unit(self, unit_id, player_id, unit_type="rifle", alive=True):
        raise NotImplementedError()

    def kill_unit(self, unit_id):
        raise NotImplementedError()

    def set_resources(self, player_id, amount):
        raise NotImplementedError()

    def issue(self, player_id, unit_id, command, client_seq, frame=0):
        raise NotImplementedError()

    def stop(self, unit_id):
        raise NotImplementedError()

    def cancel_last(self, unit_id):
        raise NotImplementedError()

    def replace_queue(self, unit_id, commands):
        raise NotImplementedError()

    def process_frame(self, frame):
        raise NotImplementedError()

    def queue(self, unit_id):
        raise NotImplementedError()

    def set_rally(self, point):
        raise NotImplementedError()

    def spawn(self, unit_type, unit_id=None):
        raise NotImplementedError()

    def select(self, player_id, unit_ids, visible):
        raise NotImplementedError()

    def form_up(self, unit_ids, anchor):
        raise NotImplementedError()

    def snapshot(self):
        raise NotImplementedError()

    def restore(self, snapshot):
        raise NotImplementedError()
