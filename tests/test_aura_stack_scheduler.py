import pytest

import aura_stack_scheduler as au


def make():
    return au.AuraScheduler()


def test_stack_mode_adds_stacks_and_refreshes_expiry():
    scheduler = make()
    scheduler.define_aura("might", "Might", max_stacks=5, duration_ms=1000,
                          stack_mode="stack", stack_bonus=2)
    scheduler.apply("e1", "might", "tank", now_ms=0)
    scheduler.apply("e2", "might", "tank", now_ms=200)
    instance = scheduler.apply("e3", "might", "tank", now_ms=400)
    assert scheduler.stacks("tank", "might") == 3
    assert scheduler.remaining_ms(instance, now_ms=400) == 1000
    assert scheduler.stat_bonus("tank") == 6


def test_refresh_mode_keeps_one_stack_but_resets_timer():
    scheduler = make()
    scheduler.define_aura("shield", "Shield", max_stacks=1, duration_ms=1000,
                          stack_mode="refresh")
    scheduler.apply("e1", "shield", "tank", now_ms=0)
    instance = scheduler.apply("e2", "shield", "tank", now_ms=500)
    assert scheduler.stacks("tank", "shield") == 1
    assert scheduler.remaining_ms(instance, now_ms=500) == 1000


def test_same_name_auras_from_different_sources_stay_separate():
    scheduler = make()
    scheduler.define_aura("bleed", "Bleed", kind="debuff", max_stacks=3,
                          duration_ms=1000, stack_mode="stack")
    scheduler.apply("e1", "bleed", "tank", source_id="rogue", now_ms=0)
    scheduler.apply("e2", "bleed", "tank", source_id="hunter", now_ms=10)
    assert len(scheduler.instances("tank")) == 2


def test_dispel_respects_aura_category():
    scheduler = make()
    scheduler.define_aura("mark", "Mark", kind="curse", duration_ms=5000)
    scheduler.define_aura("bless", "Bless", kind="buff", duration_ms=5000)
    scheduler.apply("e1", "mark", "tank", now_ms=0)
    scheduler.apply("e2", "bless", "tank", now_ms=0)
    removed = scheduler.dispel("tank", "buff", 1)
    assert removed == ["bless"]
    assert scheduler.stacks("tank", "mark") == 1
    assert scheduler.stacks("tank", "bless") == 0


def test_expiry_removes_stacks_and_bonus_together():
    scheduler = make()
    scheduler.define_aura("might", "Might", max_stacks=3, duration_ms=100,
                          stack_mode="stack", stack_bonus=2)
    for event in range(3):
        scheduler.apply("e%d" % event, "might", "tank", now_ms=0)
    scheduler.tick(200)
    assert scheduler.stacks("tank", "might") == 0
    assert scheduler.stat_bonus("tank") == 0


def test_pause_freezes_duration_on_the_logical_clock():
    scheduler = make()
    scheduler.define_aura("guard", "Guard", duration_ms=1000)
    instance = scheduler.apply("e1", "guard", "tank", now_ms=0)
    scheduler.set_paused(True)
    scheduler.tick(5000)
    assert scheduler.remaining_ms(instance, now_ms=5000) == 1000
    scheduler.set_paused(False)
    scheduler.tick(6000)
    assert scheduler.stacks("tank", "guard") == 0


def test_death_clears_auras_and_revive_does_not_double_them():
    scheduler = make()
    scheduler.define_aura("might", "Might", max_stacks=3, duration_ms=5000,
                          stack_mode="stack", stack_bonus=2)
    for event in range(3):
        scheduler.apply("e%d" % event, "might", "tank", now_ms=0)
    scheduler.kill("tank")
    assert scheduler.stat_bonus("tank") == 0
    scheduler.revive("tank")
    scheduler.apply("e9", "might", "tank", now_ms=100)
    assert scheduler.stacks("tank", "might") == 1
    assert scheduler.stat_bonus("tank") == 2


def test_haste_changes_tick_interval_without_swallowing_last_tick():
    scheduler = make()
    scheduler.define_aura("dot", "Dot", kind="debuff", duration_ms=3000,
                          tick_ms=1000)
    scheduler.apply("e1", "dot", "tank", now_ms=0)
    scheduler.set_haste("tank", 2.0)
    events = scheduler.tick(3000)
    assert len(events) == 6
    assert events[-1]["at_ms"] == 3000


def test_same_frame_expiries_are_ordered_by_instance_id():
    scheduler = make()
    scheduler.define_aura("a", "A", duration_ms=1000)
    scheduler.define_aura("b", "B", duration_ms=1000)
    scheduler.apply("e1", "b", "tank", now_ms=0)
    scheduler.apply("e2", "a", "tank", now_ms=0)
    events = scheduler.tick(1000)
    ids = [event["instance_id"] for event in events]
    assert ids == sorted(ids)
    assert len(ids) == 2


def test_snapshot_restores_remaining_time_and_stacks():
    scheduler = make()
    scheduler.define_aura("might", "Might", max_stacks=3, duration_ms=5000,
                          stack_mode="stack")
    scheduler.apply("e1", "might", "tank", now_ms=0)
    instance = scheduler.apply("e2", "might", "tank", now_ms=0)
    saved = scheduler.snapshot()
    restored = make()
    restored.restore(saved)
    assert restored.stacks("tank", "might") == 2
    assert restored.remaining_ms(instance, now_ms=1000) == 4000


def test_immunity_rejects_new_debuffs():
    scheduler = make()
    scheduler.define_aura("poison", "Poison", kind="debuff", duration_ms=1000)
    scheduler.set_immune("tank", True)
    accepted = scheduler.apply("e1", "poison", "tank", now_ms=0)
    assert accepted is False
    assert scheduler.stacks("tank", "poison") == 0


def test_thirty_third_aura_raises_instead_of_overwriting():
    scheduler = make()
    for index in range(33):
        scheduler.define_aura("aura%d" % index, "Aura%d" % index,
                              duration_ms=10000)
    for index in range(32):
        scheduler.apply("e%d" % index, "aura%d" % index, "tank", now_ms=0)
    with pytest.raises(au.AuraStateError):
        scheduler.apply("e32", "aura32", "tank", now_ms=0)


def test_travel_clears_auras_and_reapplying_does_not_double_stacks():
    scheduler = make()
    scheduler.define_aura("might", "Might", max_stacks=5, duration_ms=5000,
                          stack_mode="stack")
    for event in range(3):
        scheduler.apply("e%d" % event, "might", "tank", now_ms=0)
    scheduler.travel("tank")
    scheduler.apply("e9", "might", "tank", now_ms=10)
    assert scheduler.stacks("tank", "might") == 1


def test_unknown_aura_id_raises():
    scheduler = make()
    with pytest.raises(au.AuraStateError):
        scheduler.apply("e1", "missing", "tank", now_ms=0)


def test_duplicate_event_id_is_applied_once():
    scheduler = make()
    scheduler.define_aura("might", "Might", max_stacks=5, duration_ms=1000,
                          stack_mode="stack")
    scheduler.apply("e1", "might", "tank", now_ms=0)
    scheduler.apply("e1", "might", "tank", now_ms=0)
    assert scheduler.stacks("tank", "might") == 1
