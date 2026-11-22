class CardRuleError(Exception):
    def __init__(self, code, message=""):
        super().__init__(message or code)
        self.code = code


_RNG_MASK = (1 << 64) - 1


class PlayResolver:
    def __init__(self, seed):
        self.seed = seed
        self._players = {}
        self._hands = {}
        self._decks = {}
        self._stack = []
        self._log = []
        self._seqs = set()
        self._graveyard = {}
        self._match_over = False
        self._rng_state = int(seed) & _RNG_MASK

    def _randbelow(self, n):
        self._rng_state = (
            self._rng_state * 6364136223846793005 + 1442695040888963407
        ) & _RNG_MASK
        return (self._rng_state >> 33) % n

    def add_player(self, player_id, health=30, mana=10):
        self._players[player_id] = {
            "health": health,
            "max_health": health,
            "mana": mana,
            "shield": 0,
        }
        self._hands[player_id] = []
        self._decks[player_id] = []
        self._graveyard[player_id] = []

    def add_card(self, player_id, card):
        import copy

        self._hands[player_id].append(copy.deepcopy(card))

    def add_deck_card(self, player_id, card):
        import copy

        self._decks[player_id].append(copy.deepcopy(card))

    def _find_hand_card(self, player_id, card_id):
        for card in self._hands[player_id]:
            if card["card_id"] == card_id:
                return card
        return None

    def play(self, player_id, card_id, target=None, seq=None):
        if self._match_over:
            raise CardRuleError("MATCH_OVER", "the match has already ended")
        if seq is not None and seq in self._seqs:
            return False
        card = self._find_hand_card(player_id, card_id)
        if card is None:
            raise CardRuleError(
                "CARD_NOT_FOUND", "card %r is not in %r's hand" % (card_id, player_id)
            )
        if isinstance(target, bool) or (
            target is not None and not isinstance(target, int) and target not in self._players
        ):
            raise CardRuleError("INVALID_TARGET", "illegal target %r" % (target,))
        if isinstance(target, int) and not any(
            cmd["seq"] == target and not cmd.get("cancelled") for cmd in self._stack
        ):
            raise CardRuleError(
                "INVALID_TARGET", "no queued command with seq %r" % (target,)
            )
        cost = card.get("cost", 0)
        if cost > self._players[player_id]["mana"]:
            raise CardRuleError(
                "INSUFFICIENT_COST",
                "card %r costs %d mana" % (card_id, cost),
            )
        self._players[player_id]["mana"] -= cost
        self._hands[player_id].remove(card)
        command = {
            "player_id": player_id,
            "card_id": card_id,
            "card": card,
            "target": target,
            "seq": seq,
        }
        self._stack.append(command)
        self._log.append(
            {
                "player_id": player_id,
                "seq": seq,
                "card_id": card_id,
                "target": target,
            }
        )
        if seq is not None:
            self._seqs.add(seq)
        return True

    def resolve_next(self):
        while self._stack:
            command = self._stack.pop()
            if command.get("cancelled"):
                continue
            card = command["card"]
            target = command["target"]
            if card.get("counter") and isinstance(target, int):
                for queued in self._stack:
                    if queued["seq"] == target:
                        queued["cancelled"] = True
            elif card.get("damage") and target in self._players:
                self.damage(target, card["damage"])
            return {
                "player_id": command["player_id"],
                "seq": command["seq"],
                "card_id": command["card_id"],
                "target": target,
            }
        return None

    def end_turn(self, player_id):
        self._stack = [cmd for cmd in self._stack if cmd["player_id"] != player_id]
        self._log = [entry for entry in self._log if entry["player_id"] != player_id]

    def shuffle(self, player_id):
        deck = self._decks[player_id]
        for index in range(len(deck) - 1, 0, -1):
            swap = self._randbelow(index + 1)
            deck[index], deck[swap] = deck[swap], deck[index]
        return [card["card_id"] for card in deck]

    def draw(self, player_id, count=1):
        deck = self._decks[player_id]
        if len(deck) < count:
            raise CardRuleError(
                "DECK_EMPTY",
                "cannot draw %d card(s) from a deck of %d" % (count, len(deck)),
            )
        for _ in range(count):
            self._hands[player_id].append(deck.pop())

    def apply_cost_reduction(self, player_id, card_id, amount):
        card = self._find_hand_card(player_id, card_id)
        if card is None:
            raise CardRuleError(
                "CARD_NOT_FOUND", "card %r is not in %r's hand" % (card_id, player_id)
            )
        card["cost"] = max(0, card.get("cost", 0) - amount)

    def to_graveyard(self, player_id, card_id):
        card = self._find_hand_card(player_id, card_id)
        if card is None:
            raise CardRuleError(
                "CARD_NOT_FOUND", "card %r is not in %r's hand" % (card_id, player_id)
            )
        self._hands[player_id].remove(card)
        self._graveyard[player_id].append(card)

    def exile(self, player_id, card_id):
        for card in self._graveyard[player_id]:
            if card["card_id"] == card_id:
                self._graveyard[player_id].remove(card)
                return True
        return False

    def copy_hand(self, player_id, other_id):
        import copy

        self._hands[player_id].extend(copy.deepcopy(self._hands[other_id]))

    def set_card_cost(self, player_id, card_id, cost):
        card = self._find_hand_card(player_id, card_id)
        if card is None:
            raise CardRuleError(
                "CARD_NOT_FOUND", "card %r is not in %r's hand" % (card_id, player_id)
            )
        card["cost"] = cost

    def damage(self, player_id, amount):
        player = self._players[player_id]
        remaining = amount
        if player["shield"] > 0:
            absorbed = min(player["shield"], remaining)
            player["shield"] -= absorbed
            remaining -= absorbed
        player["health"] = max(0, player["health"] - remaining)

    def heal(self, player_id, amount):
        player = self._players[player_id]
        if player["health"] <= 0:
            return
        player["health"] = min(player["max_health"], player["health"] + amount)

    def finish_match(self):
        self._match_over = True

    def hand(self, player_id):
        return list(self._hands[player_id])

    def mana(self, player_id):
        return self._players[player_id]["mana"]

    def health(self, player_id):
        return self._players[player_id]["health"]

    def grant_shield(self, player_id, amount):
        player = self._players[player_id]
        if player["health"] <= 0:
            return
        player["shield"] += amount

    def shield(self, player_id):
        return self._players[player_id]["shield"]

    def graveyard_count(self, player_id):
        return len(self._graveyard[player_id])

    def queue(self, player_id):
        return [
            cmd
            for cmd in self._stack
            if cmd["player_id"] == player_id and not cmd.get("cancelled")
        ]

    def log(self):
        return list(self._log)

    def snapshot(self):
        import copy

        return {
            "seed": self.seed,
            "players": copy.deepcopy(self._players),
            "hands": copy.deepcopy(self._hands),
            "decks": copy.deepcopy(self._decks),
            "stack": copy.deepcopy(self._stack),
            "log": copy.deepcopy(self._log),
            "seqs": sorted(self._seqs),
            "graveyard": copy.deepcopy(self._graveyard),
            "match_over": self._match_over,
            "rng_state": self._rng_state,
        }

    def restore(self, snapshot):
        import copy

        self.seed = snapshot["seed"]
        self._players = copy.deepcopy(snapshot["players"])
        self._hands = copy.deepcopy(snapshot["hands"])
        self._decks = copy.deepcopy(snapshot["decks"])
        self._stack = copy.deepcopy(snapshot["stack"])
        self._log = copy.deepcopy(snapshot["log"])
        self._seqs = set(snapshot["seqs"])
        self._graveyard = copy.deepcopy(snapshot["graveyard"])
        self._match_over = snapshot["match_over"]
        self._rng_state = snapshot["rng_state"]
