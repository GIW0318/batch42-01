import pytest

import companion_ai_command
from companion_ai_command import CompanionAiCommand, CompanionError


def _game():
    game = CompanionAiCommand()
    game.register_pet("pet-1", "owner-1", scene_id="scene-1")
    return game


def test_rare_creature_can_only_be_tamed_once():
    game = _game()
    first = game.tame("rare-1", "owner-1")
    assert first["status"] == "tamed"
    with pytest.raises(CompanionError) as exc:
        game.tame("rare-1", "owner-2")
    assert exc.value.code == "already_tamed"
    assert game.creature_owner("rare-1") == "owner-1"


def test_command_sequence_is_global_per_owner_across_pets():
    game = _game()
    game.register_pet("pet-2", "owner-1", scene_id="scene-1")
    game.issue_command("pet-1", 5, "attack", "mob-1")
    game.issue_command("pet-2", 3, "follow")
    assert game.pending_commands("pet-2") == []
    game.issue_command("pet-2", 6, "follow")
    assert [entry["seq"] for entry in game.pending_commands("pet-2")] == [6]


def test_lower_sequence_is_dropped():
    game = _game()
    game.issue_command("pet-1", 2, "attack", "mob-1")
    game.issue_command("pet-1", 1, "follow")
    assert [entry["seq"] for entry in game.pending_commands("pet-1")] == [2]
    game.issue_command("pet-1", 3, "follow")
    assert [entry["seq"] for entry in game.pending_commands("pet-1")] == [2, 3]


def test_out_of_order_gap_does_not_replay_old_sequence():
    game = _game()
    game.issue_command("pet-1", 3, "attack", "mob-1")
    game.issue_command("pet-1", 5, "follow")
    game.issue_command("pet-1", 4, "attack", "mob-2")
    assert [entry["seq"] for entry in game.pending_commands("pet-1")] == [3, 5]


def test_migration_failure_leaves_pet_in_original_scene():
    def boom(pet_id, from_scene, to_scene):
        raise RuntimeError("scene transfer aborted")

    game = CompanionAiCommand(migration_hook=boom)
    game.register_pet("pet-1", "owner-1", scene_id="scene-1")
    with pytest.raises(CompanionError) as exc:
        game.migrate("pet-1", "scene-2")
    assert exc.value.code == "migration_failed"
    state = game.companion_state("pet-1")
    assert state["scene_id"] == "scene-1"


def test_restart_restores_scene_position_skills_and_bag():
    game = _game()
    game.learn_skill("pet-1", "dash")
    game.add_bag_item("pet-1", "bone")
    game.move_pet("pet-1", (12.5, 3.0, 8.0))
    snapshot = game.snapshot()

    restored = CompanionAiCommand()
    restored.restore(snapshot)
    state = restored.companion_state("pet-1")
    assert state["scene_id"] == "scene-1"
    assert state["position"] == (12.5, 3.0, 8.0)
    assert state["skills"] == ["dash"]
    assert restored.companion_bag("pet-1") == ["bone"]


def test_pet_death_clears_queue_and_revive_starts_idle():
    game = _game()
    game.issue_command("pet-1", 1, "attack", "mob-1")
    game.pet_died("pet-1")
    assert game.pending_commands("pet-1") == []
    assert game.companion_state("pet-1")["status"] == "dead"
    game.revive("pet-1")
    state = game.companion_state("pet-1")
    assert state["status"] == "idle"
    assert state["target"] is None
    assert game.execute_next("pet-1") is None


def test_pet_cannot_be_revived_twice():
    game = _game()
    game.pet_died("pet-1")
    first = game.revive("pet-1")
    assert first["status"] == "revived"
    with pytest.raises(CompanionError) as exc:
        game.revive("pet-1")
    assert exc.value.code == "already_alive"


def test_dead_attack_target_cancels_back_to_follow():
    game = _game()
    game.issue_command("pet-1", 1, "attack", "mob-1")
    game.execute_next("pet-1")
    game.target_died("pet-1")
    state = game.companion_state("pet-1")
    assert state["command"] == "follow"
    assert state["target"] is None
    assert game.pending_commands("pet-1") == []


def test_stun_does_not_duplicate_or_reorder_commands():
    game = _game()
    game.issue_command("pet-1", 1, "follow")
    game.stun("pet-1")
    game.issue_command("pet-1", 2, "attack", "mob-1")
    game.unstun("pet-1")
    assert [entry["seq"] for entry in game.pending_commands("pet-1")] == [1, 2]
    first = game.execute_next("pet-1")
    second = game.execute_next("pet-1")
    assert first["seq"] == 1
    assert second["seq"] == 2


def test_rename_does_not_redirect_commands_between_same_named_pets():
    game = _game()
    game.register_pet("pet-2", "owner-1", scene_id="scene-1", name="Fluffy")
    game.rename_pet("pet-1", "Fluffy")
    game.issue_command("pet-1", 1, "attack", "mob-a")
    game.issue_command("pet-2", 2, "attack", "mob-b")
    game.rename_pet("pet-1", "Rex")
    assert game.pending_commands("pet-1")[0]["target"] == "mob-a"
    assert game.pending_commands("pet-2")[0]["target"] == "mob-b"
    executed = game.execute_next("pet-2")
    assert executed["target"] == "mob-b"


def test_dismiss_and_summon_do_not_duplicate_bag_items():
    game = _game()
    game.add_bag_item("pet-1", "bone")
    game.dismiss("pet-1")
    assert game.storage_bag("owner-1") == ["bone"]
    assert game.companion_bag("pet-1") == []
    game.summon("pet-1")
    assert game.companion_bag("pet-1") == ["bone"]
    assert game.storage_bag("owner-1") == []
    game.dismiss("pet-1")
    game.summon("pet-1")
    assert game.companion_bag("pet-1").count("bone") == 1


def test_owner_death_despawns_pet_and_blocks_summon():
    game = _game()
    game.owner_died("owner-1")
    assert game.companion_state("pet-1")["status"] == "despawned"
    with pytest.raises(CompanionError) as exc:
        game.summon("pet-1")
    assert exc.value.code == "owner_dead"
    game.revive_owner("owner-1")
    summoned = game.summon("pet-1")
    assert summoned["status"] == "summoned"


def test_quest_item_pickup_goes_to_quest_log_not_pet_bag():
    game = _game()
    game.auto_pickup("pet-1", "quest-orb", quest_item=True)
    assert game.companion_bag("pet-1") == []
    assert game.quest_log("owner-1") == ["quest-orb"]
    game.auto_pickup("pet-1", "bone", quest_item=False)
    assert game.companion_bag("pet-1") == ["bone"]


def test_cross_scene_migration_clears_old_scene_buffs():
    game = _game()
    game.add_buff("pet-1", "old-scene-buff")
    game.migrate("pet-1", "scene-2")
    state = game.companion_state("pet-1")
    assert state["scene_id"] == "scene-2"
    assert "old-scene-buff" not in state["buffs"]
