class DialogGraphError(Exception):
    pass


class DialogBranch:
    def __init__(self):
        self._nodes = {}
        self._order = []
        self._quest_state = {}
        self._node_id = None
        self._line_index = 0
        self._subtitle_index = 0
        self._variables = {}
        self._visited = []
        self._rewards = []
        self._autoplay = False
        self._npc_dead = False
        self._player_id = None

    def add_node(self, node_id, text, choices=None, next_node=None,
                 priority=0, voice=None, texts=None):
        raise NotImplementedError()

    def start(self, quest_state=None, player_id="p1", node_id=None):
        raise NotImplementedError()

    def current(self):
        raise NotImplementedError()

    def available_choices(self):
        raise NotImplementedError()

    def best_choice(self):
        raise NotImplementedError()

    def choose(self, choice_id):
        raise NotImplementedError()

    def advance(self):
        raise NotImplementedError()

    def rollback(self):
        raise NotImplementedError()

    def variables(self):
        raise NotImplementedError()

    def set_variable(self, key, value):
        raise NotImplementedError()

    def rewards(self):
        raise NotImplementedError()

    def visited(self):
        raise NotImplementedError()

    def text_for(self, lang="en"):
        raise NotImplementedError()

    def skip_voice(self):
        raise NotImplementedError()

    def play_voice(self):
        raise NotImplementedError()

    def kill_npc(self):
        raise NotImplementedError()

    def set_autoplay(self, enabled):
        raise NotImplementedError()

    def snapshot(self):
        raise NotImplementedError()

    def restore(self, snapshot):
        raise NotImplementedError()
