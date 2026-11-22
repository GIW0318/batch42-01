"""Card deck legality validation, persistence and match snapshots (skeleton).

Validation runs against the current ban list version, save uses optimistic
versioning, imports reject unknown cards as a whole and a match freezes its
deck.  No behaviour is implemented yet.
"""

DECK_ILLEGAL = "deck_illegal"
STALE_DECK = "stale_deck"
UNKNOWN_CARD = "unknown_card"
DECK_NOT_FOUND = "deck_not_found"
UNSUPPORTED_FORMAT = "unsupported_format"

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

    def set_ban_list(self, cards, version=None):
        raise NotImplementedError

    def validate(self, deck):
        raise NotImplementedError

    def save(self, deck_id, deck, expected_version=0):
        raise NotImplementedError

    def load(self, deck_id):
        raise NotImplementedError

    def deck_version(self, deck_id):
        raise NotImplementedError

    def import_deck(self, payload):
        raise NotImplementedError

    def export_deck(self, deck_id):
        raise NotImplementedError

    def start_match(self, match_id, deck_id):
        raise NotImplementedError

    def match_deck(self, match_id):
        raise NotImplementedError
