class CardRuleError(Exception):
    def __init__(self, code, message=""):
        super().__init__(message or code)
        self.code = code


class PlayResolver:
    def __init__(self, seed):
        self.seed = seed
        self._players = {}
        self._hands = {}
        self._decks = {}
        self._queues = {}
        self._log = []
        self._seqs = set()
        self._graveyard = {}
        self._match_over = False
        self._rng_state = 0

    def add_player(self, player_id, health=30, mana=10):
        raise NotImplementedError()

    def add_card(self, player_id, card):
        raise NotImplementedError()

    def add_deck_card(self, player_id, card):
        raise NotImplementedError()

    def play(self, player_id, card_id, target=None, seq=None):
        raise NotImplementedError()

    def resolve_next(self):
        raise NotImplementedError()

    def end_turn(self, player_id):
        raise NotImplementedError()

    def shuffle(self, player_id):
        raise NotImplementedError()

    def draw(self, player_id, count=1):
        raise NotImplementedError()

    def apply_cost_reduction(self, player_id, card_id, amount):
        raise NotImplementedError()

    def to_graveyard(self, player_id, card_id):
        raise NotImplementedError()

    def exile(self, player_id, card_id):
        raise NotImplementedError()

    def copy_hand(self, player_id, other_id):
        raise NotImplementedError()

    def set_card_cost(self, player_id, card_id, cost):
        raise NotImplementedError()

    def damage(self, player_id, amount):
        raise NotImplementedError()

    def heal(self, player_id, amount):
        raise NotImplementedError()

    def finish_match(self):
        raise NotImplementedError()

    def hand(self, player_id):
        raise NotImplementedError()

    def mana(self, player_id):
        raise NotImplementedError()

    def health(self, player_id):
        raise NotImplementedError()

    def grant_shield(self, player_id, amount):
        raise NotImplementedError()

    def shield(self, player_id):
        raise NotImplementedError()

    def graveyard_count(self, player_id):
        raise NotImplementedError()

    def queue(self, player_id):
        raise NotImplementedError()

    def log(self):
        raise NotImplementedError()

    def snapshot(self):
        raise NotImplementedError()

    def restore(self, snapshot):
        raise NotImplementedError()
