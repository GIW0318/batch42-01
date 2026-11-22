import pytest

import deck_builder_legality
from deck_builder_legality import (
    DeckBuilderLegality,
    DeckError,
    DeckIllegalError,
    UnknownCardError,
)


def _cards():
    return {
        "spell-a": {"cost": 2, "hero": None, "mode": "standard"},
        "spell-b": {"cost": 5, "hero": None, "mode": "standard"},
        "spell-foil": {"cost": 2, "hero": None, "mode": "standard",
                       "alias": "spell-a"},
        "hero-card": {"cost": 3, "hero": "warden", "mode": "standard"},
        "wild-card": {"cost": 4, "hero": None, "mode": "wild"},
    }


def _deck(hero="warden", cards=None, sideboard=None, **extra):
    deck = {
        "hero": hero,
        "mode": "standard",
        "cards": dict(cards or {"spell-a": 2}),
        "sideboard": dict(sideboard or {}),
    }
    deck.update(extra)
    return deck


def test_banned_card_is_reported_with_rule_version():
    game = DeckBuilderLegality(card_db=_cards(), ban_list=["spell-b"],
                               ban_list_version="2026.1")
    with pytest.raises(DeckIllegalError) as exc:
        game.validate(_deck(cards={"spell-a": 2, "spell-b": 1}))
    banned = [item for item in exc.value.violations
              if item["type"] == "banned"]
    assert len(banned) == 1
    assert banned[0]["card_id"] == "spell-b"
    assert banned[0]["rules_version"] == "2026.1"


def test_three_copies_of_one_card_is_over_limit():
    game = DeckBuilderLegality(card_db=_cards())
    with pytest.raises(DeckIllegalError) as exc:
        game.validate(_deck(cards={"spell-a": 3}))
    violation = [item for item in exc.value.violations
                 if item["type"] == "over_limit"][0]
    assert violation["card_id"] == "spell-a"
    assert violation["count"] == 3
    assert violation["limit"] == 2


def test_hero_specific_card_in_other_hero_deck():
    game = DeckBuilderLegality(card_db=_cards())
    with pytest.raises(DeckIllegalError) as exc:
        game.validate(_deck(hero="ranger",
                            cards={"spell-a": 2, "hero-card": 1}))
    violation = [item for item in exc.value.violations
                 if item["type"] == "hero_mismatch"][0]
    assert violation["card_id"] == "hero-card"
    assert violation["hero"] == "warden"


def test_rotated_card_is_not_legal_in_standard():
    game = DeckBuilderLegality(card_db=_cards())
    with pytest.raises(DeckIllegalError) as exc:
        game.validate(_deck(cards={"spell-a": 2, "wild-card": 1}))
    violation = [item for item in exc.value.violations
                 if item["type"] == "not_standard"][0]
    assert violation["card_id"] == "wild-card"
    assert violation["mode"] == "wild"


def test_sideboard_over_limit_is_reported():
    cards = _cards()
    for index in range(16):
        cards["filler-%d" % index] = {
            "cost": 1, "hero": None, "mode": "standard"}
    sideboard = {"filler-%d" % index: 1 for index in range(16)}
    game = DeckBuilderLegality(card_db=cards)
    with pytest.raises(DeckIllegalError) as exc:
        game.validate(_deck(sideboard=sideboard))
    violation = [item for item in exc.value.violations
                 if item["type"] == "sideboard_limit"][0]
    assert violation["count"] == 16
    assert violation["limit"] == 15


def test_validation_is_deterministic():
    game = DeckBuilderLegality(card_db=_cards(), ban_list=["spell-b"])
    bad = _deck(cards={"spell-a": 3, "spell-b": 1})
    first = second = None
    with pytest.raises(DeckIllegalError) as exc:
        game.validate(bad)
    first = [(item["type"], item["card_id"]) for item in exc.value.violations]
    with pytest.raises(DeckIllegalError) as exc:
        game.validate(bad)
    second = [(item["type"], item["card_id"]) for item in exc.value.violations]
    assert first == second


def test_total_cost_comes_from_server_card_database():
    game = DeckBuilderLegality(card_db=_cards())
    deck = _deck(cards={"spell-a": 2, "spell-b": 1})
    deck["declared_cost"] = 1
    result = game.validate(deck)
    assert result["legal"] is True
    assert result["total_cost"] == 9


def test_save_with_stale_version_returns_stale_deck():
    game = DeckBuilderLegality(card_db=_cards())
    first = game.save("deck-1", _deck(), expected_version=0)
    assert first["version"] == 1
    with pytest.raises(DeckError) as exc:
        game.save("deck-1", _deck(cards={"spell-b": 1}), expected_version=0)
    assert exc.value.code == "stale_deck"
    assert game.deck_version("deck-1") == 1


def test_save_illegal_deck_does_not_bump_version():
    game = DeckBuilderLegality(card_db=_cards(), ban_list=["spell-b"])
    game.save("deck-1", _deck(), expected_version=0)
    with pytest.raises(DeckIllegalError):
        game.save("deck-1", _deck(cards={"spell-b": 1}), expected_version=1)
    assert game.deck_version("deck-1") == 1


def test_import_unknown_card_lists_ids_and_rejects_whole_payload():
    game = DeckBuilderLegality(card_db=_cards())
    payload = {
        "format_version": 2,
        "deck": _deck(cards={"spell-a": 2, "mystery-card": 1}),
    }
    with pytest.raises(UnknownCardError) as exc:
        game.import_deck(payload)
    assert exc.value.card_ids == ["mystery-card"]
    assert exc.value.code == "unknown_card"


def test_failed_import_keeps_existing_deck():
    game = DeckBuilderLegality(card_db=_cards())
    game.save("deck-1", _deck(), expected_version=0)
    with pytest.raises(UnknownCardError):
        game.import_deck({
            "format_version": 2,
            "deck": _deck(cards={"spell-a": 2, "mystery-card": 1}),
        })
    assert game.load("deck-1")["cards"] == {"spell-a": 2}


def test_export_and_import_round_trip_with_format_version():
    game = DeckBuilderLegality(card_db=_cards())
    deck = _deck(cards={"spell-a": 2, "spell-b": 1})
    game.save("deck-1", deck, expected_version=0)
    exported = game.export_deck("deck-1")
    assert exported["format_version"] == 2
    assert exported["deck"]["cards"] == {"spell-a": 2, "spell-b": 1}
    imported = game.import_deck(exported)
    assert imported == exported["deck"]


def test_match_snapshot_is_frozen_after_later_save():
    game = DeckBuilderLegality(card_db=_cards())
    game.save("deck-1", _deck(cards={"spell-a": 2}), expected_version=0)
    game.start_match("match-1", "deck-1")
    game.save("deck-1", _deck(cards={"spell-b": 2}), expected_version=1)
    frozen = game.match_deck("match-1")
    assert frozen["cards"] == {"spell-a": 2}


def test_foil_alias_counts_towards_same_card_limit():
    game = DeckBuilderLegality(card_db=_cards())
    with pytest.raises(DeckIllegalError) as exc:
        game.validate(_deck(cards={"spell-a": 2, "spell-foil": 1}))
    violation = [item for item in exc.value.violations
                 if item["type"] == "over_limit"][0]
    assert violation["card_id"] == "spell-a"
    assert violation["count"] == 3


def test_new_ban_list_version_applies_to_previously_legal_deck():
    game = DeckBuilderLegality(card_db=_cards())
    deck = _deck(cards={"spell-a": 2})
    assert game.validate(deck)["rules_version"] == "2026.1"
    game.set_ban_list(["spell-a"], version="2026.2")
    with pytest.raises(DeckIllegalError) as exc:
        game.validate(deck)
    banned = [item for item in exc.value.violations
              if item["type"] == "banned"][0]
    assert banned["rules_version"] == "2026.2"
