import pytest

import tournament_bracket_seeding
from tournament_bracket_seeding import BracketError, TournamentBracketSeeding


def _entrants(count, prefix="p"):
    return [
        {
            "player_id": "%s%d" % (prefix, index),
            "seed": index,
            "registered_at": "2026-01-01T00:00:%02d+00:00" % index,
        }
        for index in range(1, count + 1)
    ]


def test_byes_place_each_player_exactly_once_in_round_two():
    game = TournamentBracketSeeding(size=8)
    game.seed(_entrants(5))
    first_round = game.matches(1)
    assert len(first_round) == 4
    byes = [match for match in first_round if match["status"] == "bye"]
    assert len(byes) == 3
    round_two_players = [
        player for match in game.matches(2) for player in match["players"]
        if player is not None
    ]
    assert len(round_two_players) == 4
    assert len(set(round_two_players)) == 4


def test_same_slot_concurrent_write_keeps_one_winner():
    game = TournamentBracketSeeding(size=4)
    game.seed(_entrants(4))
    first_match, second_match = game.matches(1)
    first = game.commit_slot(2, 0, first_match["match_id"],
                             first_match["players"][0], "ref-1")
    second = game.commit_slot(2, 0, second_match["match_id"],
                              second_match["players"][0], "ref-2")
    assert first["status"] == "advanced"
    assert second["status"] == "conflict"
    assert game.slot_winner(2, 0) == first_match["players"][0]
    assert len(game.conflicts()) == 1


def test_duplicate_result_submission_advances_once():
    game = TournamentBracketSeeding(size=4)
    game.seed(_entrants(4))
    match_id = game.matches(1)[0]["match_id"]
    first = game.report_result(match_id, "2-0", "ref-1")
    second = game.report_result(match_id, "2-0", "ref-1")
    assert first["status"] == "recorded"
    assert second["status"] == "bracket_advanced"
    finalists = [
        player for match in game.matches(2) for player in match["players"]
        if player is not None
    ]
    assert len(finalists) == 1


def test_reschedule_after_finished_match_is_rejected():
    game = TournamentBracketSeeding(size=4)
    game.seed(_entrants(4))
    match_id = game.matches(1)[0]["match_id"]
    game.report_result(match_id, "2-0", "ref-1")
    with pytest.raises(BracketError) as exc:
        game.reschedule(match_id, "2026-02-01T10:00:00+00:00", "ref-2")
    assert exc.value.code == "match_finished"
    assert game.match(match_id)["status"] == "finished"


def test_restart_keeps_finished_matches_finished():
    game = TournamentBracketSeeding(size=4)
    game.seed(_entrants(4))
    match_id = game.matches(1)[0]["match_id"]
    game.report_result(match_id, "2-1", "ref-1")
    snapshot = game.snapshot()

    restored = TournamentBracketSeeding()
    restored.restore(snapshot)
    assert restored.match(match_id)["status"] == "finished"
    again = restored.report_result(match_id, "2-1", "ref-1")
    assert again["status"] == "bracket_advanced"
    finalists = [
        player for match in restored.matches(2) for player in match["players"]
        if player is not None
    ]
    assert len(finalists) == 1


def test_seed_tie_uses_utc_registration_order():
    game = TournamentBracketSeeding(size=4)
    game.seed([
        {"player_id": "late", "seed": 1,
         "registered_at": "2026-01-01T12:00:00+08:00"},
        {"player_id": "early", "seed": 1,
         "registered_at": "2026-01-01T01:00:00+00:00"},
        {"player_id": "p3", "seed": 2,
         "registered_at": "2026-01-01T03:00:00+00:00"},
        {"player_id": "p4", "seed": 3,
         "registered_at": "2026-01-01T04:00:00+00:00"},
    ])
    assert game.seeding_order()[0] == "early"
    assert game.seeding_order()[1] == "late"


def test_forfeit_cancels_reward_for_forfeiter_only():
    game = TournamentBracketSeeding(size=4)
    game.seed(_entrants(4))
    match = game.matches(1)[0]
    result = game.forfeit(match["match_id"], match["players"][1], "ref-1")
    assert result["status"] == "forfeit"
    assert game.slot_winner(2, 0) == match["players"][0]
    rewarded = [entry["player_id"] for entry in game.pending_rewards()]
    assert match["players"][0] in rewarded
    assert match["players"][1] not in rewarded


def test_duplicate_forfeit_advances_once():
    game = TournamentBracketSeeding(size=4)
    game.seed(_entrants(4))
    match = game.matches(1)[0]
    game.forfeit(match["match_id"], match["players"][1], "ref-1")
    second = game.forfeit(match["match_id"], match["players"][1], "ref-1")
    assert second["status"] == "bracket_advanced"
    finalists = [
        player for entry in game.matches(2) for player in entry["players"]
        if player is not None
    ]
    assert len(finalists) == 1


def test_reschedule_records_operator_and_old_time():
    game = TournamentBracketSeeding(size=4)
    game.seed(_entrants(4))
    match_id = game.matches(1)[0]["match_id"]
    old_start = game.match(match_id)["start_time"]
    new_start = "2026-02-02T12:00:00+00:00"
    game.reschedule(match_id, new_start, "ref-7")
    assert game.match(match_id)["start_time"] == new_start
    records = [entry for entry in game.match_history(match_id)
               if entry["action"] == "reschedule"]
    assert len(records) == 1
    assert records[0]["operator"] == "ref-7"
    assert records[0]["old_start"] == old_start
    assert records[0]["new_start"] == new_start


def test_replay_creates_new_match_and_archives_old():
    game = TournamentBracketSeeding(size=4)
    game.seed(_entrants(4))
    match_id = game.matches(1)[0]["match_id"]
    game.report_result(match_id, "2-1", "ref-1")
    replay = game.replay(match_id)
    assert replay["match_id"] != match_id
    assert replay["replay_of"] == match_id
    assert game.match(match_id)["status"] == "archived"
    history = game.match_history(match_id)
    assert any(entry["action"] == "result" for entry in history)


def test_two_referees_on_one_match_second_goes_to_conflict():
    game = TournamentBracketSeeding(size=4)
    game.seed(_entrants(4))
    match = game.matches(1)[0]
    first = game.report_result(match["match_id"], "2-0", "ref-1")
    second = game.report_result(match["match_id"], "0-2", "ref-2")
    assert first["status"] == "recorded"
    assert second["status"] == "conflict"
    assert game.slot_winner(2, 0) == match["players"][0]
    assert len(game.conflicts()) == 1


def test_third_place_and_final_use_disjoint_teams():
    game = TournamentBracketSeeding(size=4)
    game.seed(_entrants(4))
    first, second = game.matches(1)
    game.report_result(first["match_id"], "2-0", "ref-1")
    game.report_result(second["match_id"], "2-0", "ref-1")
    third = game.third_place_match()
    final = game.matches(2)[0]
    assert len(third["players"]) == 2
    assert set(third["players"]).isdisjoint(set(final["players"]))


def test_bye_match_cannot_report_and_bye_survives_round_two():
    game = TournamentBracketSeeding(size=8)
    game.seed(_entrants(5))
    bye = [match for match in game.matches(1)
           if match["status"] == "bye"][0]
    bye_player = [player for player in bye["players"] if player is not None][0]
    with pytest.raises(BracketError) as exc:
        game.report_result(bye["match_id"], "2-0", "ref-1")
    assert exc.value.code == "bye_match"
    round_two = [
        player for match in game.matches(2) for player in match["players"]
        if player is not None
    ]
    assert bye_player in round_two


def test_stale_client_view_is_rejected():
    game = TournamentBracketSeeding(size=4)
    game.seed(_entrants(4))
    version = game.current_version()
    match_id = game.matches(1)[0]["match_id"]
    game.report_result(match_id, "2-0", "ref-1")
    with pytest.raises(BracketError) as exc:
        game.view(version)
    assert exc.value.code == "stale_bracket"
    fresh = game.view(game.current_version())
    assert fresh["version"] == game.current_version()


def test_history_and_scores_survive_restart():
    game = TournamentBracketSeeding(size=4)
    game.seed(_entrants(4))
    match_id = game.matches(1)[0]["match_id"]
    game.report_result(match_id, "2-1", "ref-9")
    snapshot = game.snapshot()

    restored = TournamentBracketSeeding()
    restored.restore(snapshot)
    history = restored.match_history(match_id)
    results = [entry for entry in history if entry["action"] == "result"]
    assert results[0]["score"] == "2-1"
    assert results[0]["operator"] == "ref-9"
