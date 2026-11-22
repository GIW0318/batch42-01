import pytest

import threat_table_registry as th


def make(**kwargs):
    registry = th.ThreatRegistry(**kwargs)
    registry.add_combatant("tank", role="tank", joined_at=0)
    registry.add_combatant("dps", role="dps", joined_at=1)
    return registry


def damage(registry, seq, source, amount):
    return registry.submit_event({
        "seq": seq, "type": "damage", "source": source, "amount": amount,
    })


def heal(registry, seq, source, amount):
    return registry.submit_event({
        "seq": seq, "type": "heal", "source": source, "amount": amount,
    })


def test_taunt_forces_target_until_deadline_then_expires():
    registry = make()
    damage(registry, 1, "tank", 100)
    damage(registry, 2, "dps", 300)
    registry.flush()
    assert registry.current_target(now_ms=10) == "dps"
    registry.taunt("tank", until_ms=1000)
    assert registry.current_target(now_ms=999) == "tank"
    assert registry.current_target(now_ms=1000) == "dps"


def test_heal_threat_uses_one_point_five_multiplier():
    registry = make()
    heal(registry, 1, "dps", 100)
    registry.flush()
    assert registry.threat("dps") == pytest.approx(150.0)


def test_dead_combatant_is_removed_from_target_selection():
    registry = make()
    damage(registry, 1, "dps", 500)
    damage(registry, 2, "tank", 100)
    registry.submit_event({"seq": 3, "type": "death", "source": "dps"})
    registry.flush()
    assert registry.current_target(now_ms=10) == "tank"


def test_wipe_clears_every_combatants_threat():
    registry = make()
    damage(registry, 1, "dps", 500)
    damage(registry, 2, "tank", 100)
    registry.flush()
    registry.wipe()
    assert registry.current_target(now_ms=10) is None
    assert registry.threat("dps") == 0
    assert registry.threat("tank") == 0


def test_pet_damage_is_credited_to_owner():
    registry = make()
    registry.add_combatant("pet", role="pet", joined_at=2, owner="dps")
    damage(registry, 1, "pet", 50)
    registry.flush()
    assert registry.threat("pet") == 0
    assert registry.threat("dps") == pytest.approx(50.0)


def test_invulnerable_phase_ignores_heal_threat():
    registry = make()
    registry.set_vulnerable(False)
    accepted = heal(registry, 1, "dps", 100)
    registry.flush()
    assert accepted is False
    assert registry.threat("dps") == 0


def test_pause_freezes_threat_decay():
    registry = make(decay_per_second=10.0)
    damage(registry, 1, "tank", 100)
    registry.flush()
    registry.tick(1000)
    assert registry.threat("tank") == pytest.approx(90.0)
    registry.set_paused(True)
    registry.tick(5000)
    assert registry.threat("tank") == pytest.approx(90.0)
    registry.set_paused(False)
    registry.tick(1000)
    assert registry.threat("tank") == pytest.approx(80.0)


def test_split_add_starts_with_zero_threat():
    registry = make()
    damage(registry, 1, "tank", 500)
    registry.flush()
    registry.spawn_add("tank", "add1")
    assert registry.threat("add1") == 0
    assert registry.current_target(now_ms=10) == "tank"


def test_stealthed_combatant_is_skipped_when_picking_target():
    registry = make()
    damage(registry, 1, "tank", 100)
    damage(registry, 2, "dps", 500)
    registry.flush()
    registry.set_stealth("dps", True)
    assert registry.current_target(now_ms=10) == "tank"


def test_scene_change_clears_the_old_table():
    registry = make()
    damage(registry, 1, "tank", 100)
    registry.flush()
    registry.change_scene()
    with pytest.raises(th.UnknownCombatantError):
        registry.threat("tank")


def test_duplicate_event_sequence_is_applied_once():
    registry = make()
    assert damage(registry, 1, "tank", 100) is True
    assert damage(registry, 1, "tank", 100) is False
    registry.flush()
    assert registry.threat("tank") == pytest.approx(100.0)


def test_out_of_order_events_wait_for_the_missing_sequence():
    registry = make()
    heal(registry, 2, "dps", 100)
    registry.flush()
    assert registry.threat("dps") == 0
    damage(registry, 1, "tank", 200)
    registry.flush()
    assert registry.threat("tank") == pytest.approx(200.0)
    assert registry.threat("dps") == pytest.approx(150.0)


def test_equal_threat_uses_earliest_join_time_and_is_stable():
    registry = th.ThreatRegistry()
    registry.add_combatant("late", joined_at=5)
    registry.add_combatant("early", joined_at=1)
    damage(registry, 1, "late", 100)
    damage(registry, 2, "early", 100)
    registry.flush()
    assert registry.current_target(now_ms=10) == "early"
    assert registry.current_target(now_ms=20) == "early"


def test_snapshot_restore_does_not_replay_settled_events():
    registry = make()
    damage(registry, 1, "tank", 100)
    registry.flush()
    saved = registry.snapshot()
    restored = th.ThreatRegistry()
    restored.restore(saved)
    assert restored.threat("tank") == pytest.approx(100.0)
    assert damage(restored, 1, "tank", 100) is False
    assert restored.threat("tank") == pytest.approx(100.0)


def test_unknown_combatant_event_raises():
    registry = make()
    with pytest.raises(th.UnknownCombatantError):
        damage(registry, 1, "ghost", 10)
