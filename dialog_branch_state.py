class DialogGraphError(Exception):
    pass


import copy
import os


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
        self._claimed = set()
        self._history = []

    def add_node(self, node_id, text, choices=None, next_node=None,
                 priority=0, voice=None, texts=None):
        if not node_id:
            raise DialogGraphError("node_id is required")
        if node_id in self._nodes:
            raise DialogGraphError("duplicate node: %s" % node_id)
        normalized = []
        for choice in choices or []:
            if "choice_id" not in choice:
                raise DialogGraphError("choice without choice_id on %s" % node_id)
            normalized.append({
                "choice_id": choice["choice_id"],
                "target": choice.get("target"),
                "priority": choice.get("priority", 0),
                "condition": choice.get("condition"),
                "reward": choice.get("reward"),
            })
        self._nodes[node_id] = {
            "node_id": node_id,
            "text": text,
            "texts": dict(texts or {}),
            "choices": normalized,
            "next_node": next_node,
            "priority": priority,
            "voice": voice,
        }
        self._order.append(node_id)

    def start(self, quest_state=None, player_id="p1", node_id=None):
        if node_id is None:
            if not self._order:
                raise DialogGraphError("cannot start an empty dialog graph")
            node_id = self._order[0]
        if node_id not in self._nodes:
            raise DialogGraphError("unknown start node: %s" % node_id)
        self._quest_state = copy.deepcopy(quest_state) if quest_state else {}
        self._player_id = player_id
        self._node_id = node_id
        self._line_index = 0
        self._subtitle_index = 0
        self._variables = {}
        self._visited = [node_id]
        self._rewards = []
        self._claimed = set()
        self._autoplay = False
        self._npc_dead = False
        self._history = [self._capture()]

    def current(self):
        self._require_started()
        node = self._nodes[self._node_id]
        return {
            "node_id": self._node_id,
            "text": node["text"],
            "line_index": self._line_index,
            "subtitle_index": self._subtitle_index,
            "player_id": self._player_id,
        }

    def available_choices(self):
        self._require_started()
        node = self._nodes[self._node_id]
        return [dict(choice) for choice in node["choices"]
                if self._condition_ok(choice.get("condition"))]

    def best_choice(self):
        choices = self.available_choices()
        if not choices:
            return None
        return max(choices, key=lambda choice: choice["priority"])

    def choose(self, choice_id):
        self._require_started()
        if self._npc_dead:
            raise DialogGraphError("conversation ended: NPC is dead")
        if choice_id in self._claimed:
            return False
        node = self._nodes[self._node_id]
        choice = None
        for candidate in node["choices"]:
            if candidate["choice_id"] == choice_id:
                choice = candidate
                break
        if choice is None:
            return False
        if not self._condition_ok(choice.get("condition")):
            return False
        target = choice.get("target")
        if target not in self._nodes:
            raise DialogGraphError(
                "choice %s targets missing node: %s" % (choice_id, target))
        if choice.get("reward") is not None:
            self._rewards.append(choice["reward"])
        self._claimed.add(choice_id)
        self._node_id = target
        self._visited.append(target)
        self._line_index = 0
        self._subtitle_index = 0
        self._history.append(self._capture())
        return True

    def advance(self):
        self._require_started()
        if self._npc_dead:
            raise DialogGraphError("conversation ended: NPC is dead")
        self._line_index += 1
        self._subtitle_index += 1
        return self.current()

    def rollback(self):
        self._require_started()
        if len(self._history) > 1:
            self._history.pop()
        self._apply(self._history[-1])

    def variables(self):
        return dict(self._variables)

    def set_variable(self, key, value):
        self._require_started()
        self._variables[key] = value

    def rewards(self):
        return list(self._rewards)

    def visited(self):
        return list(self._visited)

    def text_for(self, lang="en"):
        self._require_started()
        node = self._nodes[self._node_id]
        localized = node["texts"].get(lang)
        if localized is None:
            return node["text"]
        return localized

    def skip_voice(self):
        self._require_started()
        self._line_index += 1
        self._subtitle_index += 1
        return self.current()

    def play_voice(self):
        self._require_started()
        voice = self._nodes[self._node_id]["voice"]
        if voice is None:
            return False
        if not os.path.exists(voice):
            raise DialogGraphError("missing voice file: %s" % voice)
        return True

    def kill_npc(self):
        self._npc_dead = True

    def set_autoplay(self, enabled):
        self._autoplay = bool(enabled)

    def snapshot(self):
        self._require_started()
        return {
            "node_id": self._node_id,
            "line_index": self._line_index,
            "subtitle_index": self._subtitle_index,
            "variables": copy.deepcopy(self._variables),
            "visited": list(self._visited),
            "rewards": list(self._rewards),
            "claimed_choices": sorted(self._claimed),
            "quest_state": copy.deepcopy(self._quest_state),
            "player_id": self._player_id,
            "autoplay": self._autoplay,
            "npc_dead": self._npc_dead,
        }

    def restore(self, snapshot):
        if not isinstance(snapshot, dict):
            raise DialogGraphError("invalid snapshot")
        node_id = snapshot.get("node_id")
        if node_id not in self._nodes:
            raise DialogGraphError("snapshot references missing node: %s"
                                   % node_id)
        self._node_id = node_id
        self._line_index = snapshot.get("line_index", 0)
        self._subtitle_index = snapshot.get("subtitle_index", 0)
        self._variables = copy.deepcopy(snapshot.get("variables") or {})
        self._visited = list(snapshot.get("visited") or [node_id])
        self._rewards = list(snapshot.get("rewards") or [])
        self._claimed = set(snapshot.get("claimed_choices") or [])
        self._quest_state = copy.deepcopy(snapshot.get("quest_state") or {})
        self._player_id = snapshot.get("player_id")
        self._autoplay = bool(snapshot.get("autoplay", False))
        self._npc_dead = bool(snapshot.get("npc_dead", False))
        self._history = [self._capture()]

    def _require_started(self):
        if self._node_id is None:
            raise DialogGraphError("dialog has not been started")

    def _condition_ok(self, condition):
        if condition is None:
            return True
        if not isinstance(condition, dict) or "quest" not in condition:
            raise DialogGraphError("invalid condition expression: %r"
                                   % (condition,))
        value = self._quest_state.get(condition["quest"])
        if "equals" in condition:
            return value == condition["equals"]
        if "not_equals" in condition:
            return value != condition["not_equals"]
        raise DialogGraphError("invalid condition expression: %r"
                               % (condition,))

    def _capture(self):
        return {
            "node_id": self._node_id,
            "line_index": self._line_index,
            "subtitle_index": self._subtitle_index,
            "variables": copy.deepcopy(self._variables),
            "visited": list(self._visited),
            "rewards": list(self._rewards),
            "claimed": set(self._claimed),
        }

    def _apply(self, state):
        self._node_id = state["node_id"]
        self._line_index = state["line_index"]
        self._subtitle_index = state["subtitle_index"]
        self._variables = copy.deepcopy(state["variables"])
        self._visited = list(state["visited"])
        self._rewards = list(state["rewards"])
        self._claimed = set(state["claimed"])
