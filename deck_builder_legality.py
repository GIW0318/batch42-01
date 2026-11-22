"""Card deck legality validation, persistence and match snapshots.

Validation runs against the current ban list version, save uses optimistic
versioning, imports reject unknown cards as a whole and a match freezes its
deck.
"""

import copy
import threading

DECK_ILLEGAL = "deck_illegal"
STALE_DECK = "stale_deck"
UNKNOWN_CARD = "unknown_card"
DECK_NOT_FOUND = "deck_not_found"
UNSUPPORTED_FORMAT = "unsupported_format"
MATCH_NOT_FOUND = "match_not_found"

DECK_FORMAT_VERSION = 2


class DeckError(Exception):
    def __init__(self, code, message=""):
        self.code = code
        super().__init__(code if not message else "%s: %s" % (code, message))


class DeckIllegalError(DeckError):
    def __init__(self, violations, code=DECK_ILLEGAL, message=""):
        self.violations = list(violations)
        super().__init__(code, message)


class UnknownCardError(DeckError):
    def __init__(self, card_ids, code=UNKNOWN_CARD, message=""):
        self.card_ids = list(card_ids)
        super().__init__(code, message)


class DeckBuilderLegality:
    def __init__(self, card_db=None, ban_list=(), ban_list_version="2026.1",
                 max_copies=2, sideboard_limit=15):
        self.card_db = dict(card_db or {})
        self.ban_list = list(ban_list)
        self.ban_list_version = ban_list_version
        self.max_copies = max_copies
        self.sideboard_limit = sideboard_limit
        self._decks = {}
        self._matches = {}
        self._lock = threading.Lock()

    def set_ban_list(self, cards, version=None):
        self.ban_list = list(cards)
        if version is not None:
            self.ban_list_version = version

    def _canonical_id(self, card_id):
        entry = self.card_db.get(card_id)
        if entry is None:
            return card_id
        return entry.get("alias", card_id)

    def _violation(self, vtype, **fields):
        violation = {"type": vtype, "rules_version": self.ban_list_version}
        violation.update(fields)
        return violation

    def _iter_cards(self, deck):
        for card_id in sorted(deck.get("cards", {})):
            yield card_id, deck["cards"][card_id]
        for card_id in sorted(deck.get("sideboard", {})):
            yield card_id, deck["sideboard"][card_id]

    def validate(self, deck):
        violations = []
        banned = set(self.ban_list)
        deck_hero = deck.get("hero")
        deck_mode = deck.get("mode", "standard")
        copies = {}
        total_cost = 0

        for card_id, count in self._iter_cards(deck):
            entry = self.card_db.get(card_id)
            if entry is None:
                violations.append(self._violation(
                    "unknown_card", card_id=card_id))
                continue
            canonical = self._canonical_id(card_id)
            copies[canonical] = copies.get(canonical, 0) + count
            if card_id in banned or canonical in banned:
                violations.append(self._violation(
                    "banned", card_id=canonical))
            card_hero = entry.get("hero")
            if card_hero is not None and card_hero != deck_hero:
                violations.append(self._violation(
                    "hero_mismatch", card_id=card_id, hero=card_hero))
            card_mode = entry.get("mode", "standard")
            if deck_mode == "standard" and card_mode != "standard":
                violations.append(self._violation(
                    "not_standard", card_id=card_id, mode=card_mode))

        for card_id in sorted(deck.get("cards", {})):
            entry = self.card_db.get(card_id)
            if entry is not None:
                total_cost += entry.get("cost", 0) * deck["cards"][card_id]

        for canonical in sorted(copies):
            if copies[canonical] > self.max_copies:
                violations.append(self._violation(
                    "over_limit", card_id=canonical,
                    count=copies[canonical], limit=self.max_copies))

        sideboard_count = sum(deck.get("sideboard", {}).values())
        if sideboard_count > self.sideboard_limit:
            violations.append(self._violation(
                "sideboard_limit", count=sideboard_count,
                limit=self.sideboard_limit))

        if violations:
            raise DeckIllegalError(violations)
        return {
            "legal": True,
            "total_cost": total_cost,
            "rules_version": self.ban_list_version,
        }

    def save(self, deck_id, deck, expected_version=0):
        with self._lock:
            current = self._decks.get(deck_id)
            current_version = current["version"] if current else 0
            if expected_version != current_version:
                raise DeckError(
                    STALE_DECK,
                    "expected version %r but deck %r is at version %r"
                    % (expected_version, deck_id, current_version))
            self.validate(deck)
            new_version = current_version + 1
            self._decks[deck_id] = {
                "version": new_version,
                "deck": copy.deepcopy(deck),
            }
            return {"deck_id": deck_id, "version": new_version}

    def load(self, deck_id):
        entry = self._decks.get(deck_id)
        if entry is None:
            raise DeckError(DECK_NOT_FOUND, "no deck %r" % deck_id)
        return copy.deepcopy(entry["deck"])

    def deck_version(self, deck_id):
        entry = self._decks.get(deck_id)
        return entry["version"] if entry else 0

    def import_deck(self, payload):
        if payload.get("format_version") != DECK_FORMAT_VERSION:
            raise DeckError(
                UNSUPPORTED_FORMAT,
                "format_version %r is not supported" %
                payload.get("format_version"))
        deck = payload.get("deck")
        if not isinstance(deck, dict):
            raise DeckError(UNSUPPORTED_FORMAT, "payload has no deck")
        unknown = sorted({
            card_id
            for card_id, _count in self._iter_cards(deck)
            if card_id not in self.card_db
        })
        if unknown:
            raise UnknownCardError(unknown)
        self.validate(deck)
        return copy.deepcopy(deck)

    def export_deck(self, deck_id):
        return {
            "format_version": DECK_FORMAT_VERSION,
            "deck": self.load(deck_id),
        }

    def start_match(self, match_id, deck_id):
        deck = self.load(deck_id)
        self.validate(deck)
        self._matches[match_id] = copy.deepcopy(deck)
        return {"match_id": match_id, "deck_id": deck_id,
                "rules_version": self.ban_list_version}

    def match_deck(self, match_id):
        snapshot = self._matches.get(match_id)
        if snapshot is None:
            raise DeckError(MATCH_NOT_FOUND, "no match %r" % match_id)
        return copy.deepcopy(snapshot)
