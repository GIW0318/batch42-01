import pytest

import dialog_branch_state as dlg


def graph():
    branch = dlg.DialogBranch()
    branch.add_node("n1", "Line one", texts={"en": "Line one"},
                    voice="intro.wav",
                    choices=[
                        {"choice_id": "to_two", "target": "n2", "priority": 1},
                        {"choice_id": "to_three", "target": "n3", "priority": 2},
                    ])
    branch.add_node("n2", "Line two")
    branch.add_node("n3", "Line three")
    return branch


def test_save_and_load_resume_the_current_node():
    branch = graph()
    branch.start()
    branch.choose("to_two")
    saved = branch.snapshot()
    restored = graph()
    restored.restore(saved)
    assert restored.current()["node_id"] == "n2"
    assert restored.visited() == ["n1", "n2"]


def test_conditions_read_the_quest_snapshot_taken_at_start():
    branch = dlg.DialogBranch()
    branch.add_node("n1", "Start", choices=[
        {"choice_id": "done_only", "target": "n2", "priority": 1,
         "condition": {"quest": "q1", "equals": "done"}},
        {"choice_id": "open", "target": "n3", "priority": 0},
    ])
    branch.add_node("n2", "Done")
    branch.add_node("n3", "Open")
    quest_state = {"q1": "incomplete"}
    branch.start(quest_state)
    quest_state["q1"] = "done"
    ids = [choice["choice_id"] for choice in branch.available_choices()]
    assert ids == ["open"]


def test_highest_priority_branch_wins_over_lexicographic_order():
    branch = graph()
    branch.start()
    assert branch.best_choice()["choice_id"] == "to_three"


def test_rollback_removes_branch_variables():
    branch = graph()
    branch.start()
    branch.set_variable("branch_flag", 1)
    assert branch.variables()["branch_flag"] == 1
    branch.rollback()
    assert "branch_flag" not in branch.variables()


def test_skip_voice_advances_audio_and_subtitle_together():
    branch = dlg.DialogBranch()
    branch.add_node("n1", "A", voice="a.wav")
    branch.start()
    branch.skip_voice()
    current = branch.current()
    assert current["line_index"] == 1
    assert current["subtitle_index"] == 1


def test_missing_localization_falls_back_to_default_text():
    branch = graph()
    branch.start()
    assert branch.text_for("fr") == "Line one"
    assert branch.text_for("fr") != "dialog_042"


def test_npc_death_stops_the_conversation():
    branch = graph()
    branch.start()
    branch.kill_npc()
    with pytest.raises(dlg.DialogGraphError):
        branch.advance()


def test_autoplay_and_manual_advance_only_one_line():
    branch = graph()
    branch.start()
    branch.set_autoplay(True)
    branch.advance()
    assert branch.current()["line_index"] == 1


def test_rollback_after_choice_revokes_the_reward():
    branch = dlg.DialogBranch()
    branch.add_node("n1", "Start", choices=[
        {"choice_id": "take", "target": "n2", "priority": 1,
         "reward": "gold"},
    ])
    branch.add_node("n2", "End")
    branch.start()
    branch.choose("take")
    assert branch.rewards() == ["gold"]
    branch.rollback()
    assert branch.rewards() == []


def test_branch_variable_does_not_leak_into_sibling_branch():
    branch = dlg.DialogBranch()
    branch.add_node("n1", "Start", choices=[
        {"choice_id": "a", "target": "na", "priority": 2},
        {"choice_id": "b", "target": "nb", "priority": 1},
    ])
    branch.add_node("na", "A")
    branch.add_node("nb", "B")
    branch.start()
    branch.choose("a")
    branch.set_variable("only_a", True)
    branch.rollback()
    branch.choose("b")
    assert "only_a" not in branch.variables()


def test_missing_voice_file_raises_instead_of_hanging():
    branch = dlg.DialogBranch()
    branch.add_node("n1", "A", voice="missing.wav")
    branch.start()
    with pytest.raises(dlg.DialogGraphError):
        branch.play_voice()


def test_two_players_do_not_share_node_state():
    first = graph()
    second = graph()
    first.start(player_id="p1")
    second.start(player_id="p2")
    first.choose("to_two")
    assert first.current()["node_id"] == "n2"
    assert second.current()["node_id"] == "n1"


def test_duplicate_choice_submission_advances_once():
    branch = graph()
    branch.start()
    assert branch.choose("to_two") is True
    assert branch.choose("to_two") is False
    assert branch.current()["node_id"] == "n2"
    assert branch.visited() == ["n1", "n2"]


def test_choice_with_missing_target_raises():
    branch = dlg.DialogBranch()
    branch.add_node("n1", "Start", choices=[
        {"choice_id": "broken", "target": "missing", "priority": 1},
    ])
    branch.start()
    with pytest.raises(dlg.DialogGraphError):
        branch.choose("broken")


def test_snapshot_keeps_claimed_rewards_across_restore():
    branch = dlg.DialogBranch()
    branch.add_node("n1", "Start", choices=[
        {"choice_id": "take", "target": "n2", "priority": 1,
         "reward": "gold"},
    ])
    branch.add_node("n2", "End")
    branch.start()
    branch.choose("take")
    saved = branch.snapshot()
    restored = graph()
    restored.restore(saved)
    assert restored.rewards() == ["gold"]
    assert restored.choose("take") is False
