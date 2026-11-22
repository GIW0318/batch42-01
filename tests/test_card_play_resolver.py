import pytest

import card_play_resolver as cr

CARD = {"card_id": "spell", "cost": 3, "type": "spell", "damage": 2}


def make(seed=7):
    resolver = cr.PlayResolver(seed=seed)
    resolver.add_player("p1", health=30, mana=10)
    resolver.add_player("p2", health=30, mana=10)
    return resolver


def test_two_spells_deduct_cost_for_both_cards():
    resolver = make()
    resolver.add_card("p1", dict(CARD, card_id="a", cost=3))
    resolver.add_card("p1", dict(CARD, card_id="b", cost=3))
    resolver.play("p1", "a", target="p2", seq=1)
    resolver.play("p1", "b", target="p2", seq=2)
    assert resolver.mana("p1") == 4


def test_duplicate_command_sequence_is_applied_once():
    resolver = make()
    resolver.add_card("p1", dict(CARD, card_id="a", cost=3))
    assert resolver.play("p1", "a", target="p2", seq=1) is True
    assert resolver.play("p1", "a", target="p2", seq=1) is False
    assert resolver.mana("p1") == 7
    assert len(resolver.log()) == 1


def test_resolution_follows_play_order_not_card_id_order():
    resolver = make()
    resolver.add_card("p1", dict(CARD, card_id="z_card", cost=1))
    resolver.add_card("p1", dict(CARD, card_id="a_card", cost=1))
    resolver.play("p1", "z_card", target="p2", seq=1)
    resolver.play("p1", "a_card", target="p2", seq=2)
    assert [entry["card_id"] for entry in resolver.log()] == ["z_card", "a_card"]


def test_counter_cancels_the_targeted_queued_spell():
    resolver = make()
    resolver.add_card("p1", dict(CARD, card_id="fire", cost=2, damage=5))
    resolver.add_card("p2", dict(CARD, card_id="counter", cost=2, counter=True))
    resolver.play("p1", "fire", target="p2", seq=1)
    resolver.play("p2", "counter", target=1, seq=2)
    assert resolver.resolve_next()["card_id"] == "counter"
    assert resolver.resolve_next() is None
    assert resolver.health("p2") == 30


def test_shuffle_is_deterministic_for_the_same_server_seed():
    first = make(seed=99)
    second = make(seed=99)
    for index in range(6):
        card = dict(CARD, card_id="c%d" % index)
        first.add_deck_card("p1", card)
        second.add_deck_card("p1", card)
    assert first.shuffle("p1") == second.shuffle("p1")


def test_snapshot_restores_random_sequence_state():
    resolver = make(seed=123)
    for index in range(6):
        resolver.add_deck_card("p1", dict(CARD, card_id="c%d" % index))
    resolver.shuffle("p1")
    saved = resolver.snapshot()
    restored = make(seed=123)
    restored.restore(saved)
    assert restored.shuffle("p1") == resolver.shuffle("p1")


def test_insufficient_cost_is_atomic_and_reports_code():
    resolver = make()
    resolver.add_card("p1", dict(CARD, card_id="big", cost=99))
    with pytest.raises(cr.CardRuleError) as info:
        resolver.play("p1", "big", target="p2", seq=1)
    assert info.value.code == "INSUFFICIENT_COST"
    assert resolver.mana("p1") == 10
    assert [card["card_id"] for card in resolver.hand("p1")] == ["big"]


def test_illegal_target_reports_its_own_code():
    resolver = make()
    resolver.add_card("p1", dict(CARD, card_id="fire", cost=1))
    with pytest.raises(cr.CardRuleError) as info:
        resolver.play("p1", "fire", target="nobody", seq=1)
    assert info.value.code == "INVALID_TARGET"
    assert resolver.mana("p1") == 10


def test_play_after_match_end_reports_own_code():
    resolver = make()
    resolver.add_card("p1", dict(CARD, card_id="fire", cost=1))
    resolver.finish_match()
    with pytest.raises(cr.CardRuleError) as info:
        resolver.play("p1", "fire", target="p2", seq=1)
    assert info.value.code == "MATCH_OVER"


def test_turn_timeout_does_not_replay_a_queued_card():
    resolver = make()
    resolver.add_card("p1", dict(CARD, card_id="fire", cost=1))
    resolver.play("p1", "fire", target="p2", seq=1)
    resolver.end_turn("p1")
    assert resolver.queue("p1") == []
    assert resolver.resolve_next() is None
    assert resolver.log() == []


def test_exile_decrements_graveyard_count():
    resolver = make()
    resolver.add_card("p1", dict(CARD, card_id="dead", cost=1))
    resolver.to_graveyard("p1", "dead")
    assert resolver.graveyard_count("p1") == 1
    assert resolver.exile("p1", "dead") is True
    assert resolver.graveyard_count("p1") == 0


def test_copy_hand_makes_independent_card_copies():
    resolver = make()
    resolver.add_card("p1", dict(CARD, card_id="origin", cost=3))
    resolver.copy_hand("p2", "p1")
    resolver.set_card_cost("p1", "origin", 1)
    copied = [card for card in resolver.hand("p2") if card["card_id"] == "origin"][0]
    assert copied["cost"] == 3


def test_draw_from_empty_deck_raises_instead_of_crashing():
    resolver = make()
    with pytest.raises(cr.CardRuleError) as info:
        resolver.draw("p1", 1)
    assert info.value.code == "DECK_EMPTY"


def test_zero_health_ignores_later_heal_and_shield():
    resolver = make()
    resolver.damage("p2", 100)
    assert resolver.health("p2") == 0
    resolver.heal("p2", 10)
    resolver.grant_shield("p2", 10)
    assert resolver.health("p2") == 0
    assert resolver.shield("p2") == 0


def test_double_cost_reduction_never_goes_negative():
    resolver = make()
    resolver.add_card("p1", dict(CARD, card_id="cheap", cost=5))
    resolver.apply_cost_reduction("p1", "cheap", 3)
    resolver.apply_cost_reduction("p1", "cheap", 3)
    cost = [card for card in resolver.hand("p1") if card["card_id"] == "cheap"][0]["cost"]
    assert cost == 0
