import threading

import pet_duel_turn_arbiter as pda


def skill(power=0, priority=0, cooldown=0, energy=0, effect=None, crit_chance=0.0):
    data = {
        "power": power,
        "priority": priority,
        "cooldown": cooldown,
        "energy": energy,
    }
    if effect is not None:
        data["effect"] = effect
    if crit_chance:
        data["crit_chance"] = crit_chance
    return data


def pet(pet_id, hp=100, energy=100, speed=10, skills=None):
    return {
        "id": pet_id,
        "hp": hp,
        "energy": energy,
        "speed": speed,
        "skills": skills or {"jab": skill(power=5)},
    }


def new_arbiter(**kwargs):
    return pda.PetDuelTurnArbiter(**kwargs)


def start(arbiter, pets_a, pets_b, match_id="m1"):
    arbiter.create_match(match_id, "p1", "p2", pets_a, pets_b)
    return match_id


def commit(arbiter, match_id, turn, skill_name="jab"):
    arbiter.submit(match_id, "p1", turn, {"type": "skill", "skill": skill_name})
    arbiter.submit(match_id, "p2", turn, {"type": "skill", "skill": skill_name})


def test_duplicate_submission_is_rejected():
    arbiter = new_arbiter()
    start(arbiter, [pet("a1")], [pet("b1")])
    first = arbiter.submit(
        "m1", "p1", 1, {"type": "skill", "skill": "jab"}
    )
    duplicate = arbiter.submit(
        "m1", "p1", 1, {"type": "skill", "skill": "jab"}
    )
    assert first["status"] == "accepted"
    assert duplicate["status"] == "turn_already_committed"


def test_switch_resolves_before_skill_and_retargets_attack():
    arbiter = new_arbiter()
    pets_a = [
        pet("a1", speed=20, skills={"jab": skill(power=5)}),
        pet("a2", speed=1, hp=100, skills={"jab": skill(power=5)}),
    ]
    pets_b = [pet("b1", speed=5, skills={"hit": skill(power=10)})]
    start(arbiter, pets_a, pets_b)
    arbiter.submit("m1", "p1", 1, {"type": "switch", "pet": "a2"})
    arbiter.submit("m1", "p2", 1, {"type": "skill", "skill": "hit"})
    result = arbiter.resolve("m1", 1)
    assert result["status"] == "resolved"
    assert result["hp"]["p1"] == 90
    assert arbiter.pet_state("m1", "p1", "a1")["hp"] == 100
    assert arbiter.pet_state("m1", "p1", "a2")["hp"] == 90


def test_priority_beats_speed_then_speed_breaks_ties():
    arbiter = new_arbiter()
    pets_a = [pet("a1", speed=5, skills={"high": skill(power=5, priority=2)})]
    pets_b = [pet("b1", speed=20, skills={"low": skill(power=5, priority=0)})]
    start(arbiter, pets_a, pets_b)
    commit(arbiter, "m1", 1)
    arbiter.resolve("m1", 1)
    log = arbiter.turn_log("m1", 1)
    assert log["action_order"][0]["player"] == "p1"
    assert log["action_order"][0]["skill"] == "high"
    assert log["action_order"][1]["player"] == "p2"


def test_same_seed_is_frame_rate_independent_over_many_replays():
    outcomes = set()
    for _ in range(100):
        arbiter = new_arbiter(seed=7)
        pets_a = [pet("a1", skills={"crit": skill(power=10, crit_chance=0.5)})]
        pets_b = [pet("b1", skills={"crit": skill(power=10, crit_chance=0.5)})]
        start(arbiter, pets_a, pets_b)
        commit(arbiter, "m1", 1, "crit")
        result = arbiter.resolve("m1", 1, frame_rate=30)
        outcomes.add((result["hp"]["p1"], result["hp"]["p2"]))
    arbiter_60 = new_arbiter(seed=7)
    pets_a = [pet("a1", skills={"crit": skill(power=10, crit_chance=0.5)})]
    pets_b = [pet("b1", skills={"crit": skill(power=10, crit_chance=0.5)})]
    start(arbiter_60, pets_a, pets_b)
    commit(arbiter_60, "m1", 1, "crit")
    result_60 = arbiter_60.resolve("m1", 1, frame_rate=60)
    assert outcomes == {(result_60["hp"]["p1"], result_60["hp"]["p2"])}


def test_speed_buff_has_a_fixed_resolution_phase():
    arbiter = new_arbiter()
    pets_a = [
        pet(
            "a1",
            speed=10,
            skills={
                "haste": skill(
                    power=0,
                    effect={"speed": 10, "turns": 2},
                ),
                "jab": skill(power=5),
            },
        )
    ]
    pets_b = [pet("b1", speed=11, skills={"jab": skill(power=5)})]
    start(arbiter, pets_a, pets_b)

    arbiter.submit("m1", "p1", 1, {"type": "skill", "skill": "haste"})
    arbiter.submit("m1", "p2", 1, {"type": "skill", "skill": "jab"})
    arbiter.resolve("m1", 1)

    commit(arbiter, "m1", 2)
    arbiter.resolve("m1", 2)
    commit(arbiter, "m1", 3)
    arbiter.resolve("m1", 3)
    commit(arbiter, "m1", 4)
    arbiter.resolve("m1", 4)

    assert arbiter.turn_log("m1", 2)["action_order"][0]["player"] == "p1"
    assert arbiter.turn_log("m1", 3)["action_order"][0]["player"] == "p1"
    assert arbiter.turn_log("m1", 4)["action_order"][0]["player"] == "p2"


def test_stealth_covers_its_expiry_turn_then_disappears():
    arbiter = new_arbiter()
    pets_a = [
        pet(
            "a1",
            skills={
                "vanish": skill(
                    power=0, effect={"stealth": True, "turns": 1}
                ),
                "jab": skill(power=5),
            },
        )
    ]
    pets_b = [pet("b1", skills={"jab": skill(power=10)})]
    start(arbiter, pets_a, pets_b)

    arbiter.submit("m1", "p1", 1, {"type": "skill", "skill": "vanish"})
    arbiter.submit("m1", "p2", 1, {"type": "skill", "skill": "jab"})
    first = arbiter.resolve("m1", 1)
    assert first["hp"]["p1"] == 90

    commit(arbiter, "m1", 2)
    second = arbiter.resolve("m1", 2)
    assert second["hp"]["p1"] == 90
    miss = [
        item
        for item in second["actions"]
        if item["player"] == "p2" and item.get("miss")
    ]
    assert len(miss) == 1

    commit(arbiter, "m1", 3)
    third = arbiter.resolve("m1", 3)
    assert third["hp"]["p1"] == 80


def test_double_knockout_is_draw_without_rating_loss():
    arbiter = new_arbiter()
    pets_a = [pet("a1", hp=10, speed=5, skills={"smash": skill(power=20)})]
    pets_b = [pet("b1", hp=10, speed=7, skills={"smash": skill(power=20)})]
    start(arbiter, pets_a, pets_b)
    commit(arbiter, "m1", 1, "smash")
    result = arbiter.resolve("m1", 1)
    assert result["result"] == "draw"
    assert result["winner"] is None
    assert result["hp"] == {"p1": 0, "p2": 0}
    assert result["rating_changes"] == {"p1": 0, "p2": 0}
    assert arbiter.rating("p1") == 1000
    assert arbiter.rating("p2") == 1000


def test_surrender_and_last_hit_apply_one_result_once():
    arbiter = new_arbiter()
    pets_a = [pet("a1", hp=10, skills={"jab": skill(power=20)})]
    pets_b = [pet("b1", hp=10, skills={"jab": skill(power=20)})]
    start(arbiter, pets_a, pets_b)
    arbiter.surrender("m1", "p1")
    arbiter.submit("m1", "p2", 1, {"type": "skill", "skill": "jab"})
    result = arbiter.resolve("m1", 1)
    assert result["result"] == "win"
    assert result["winner"] == "p2"
    assert arbiter.rating("p1") == 975
    assert arbiter.rating("p2") == 1025
    again = arbiter.resolve("m1", 1)
    assert again["status"] == "match_finished"
    assert arbiter.rating("p1") == 975
    assert arbiter.rating("p2") == 1025


def test_ai_commands_respect_cooldowns():
    arbiter = new_arbiter()
    pets_a = [
        pet(
            "a1",
            skills={
                "big": skill(power=30, cooldown=2),
                "jab": skill(power=5),
            },
        )
    ]
    pets_b = [pet("b1", skills={"jab": skill(power=5)})]
    start(arbiter, pets_a, pets_b)
    arbiter.submit("m1", "p1", 1, {"type": "skill", "skill": "big"})
    arbiter.submit("m1", "p2", 1, {"type": "skill", "skill": "jab"})
    arbiter.resolve("m1", 1)
    arbiter.take_over("m1", "p1")
    arbiter.submit("m1", "p2", 2, {"type": "skill", "skill": "jab"})
    arbiter.resolve("m1", 2)
    log = arbiter.turn_log("m1", 2)
    p1_actions = [
        item for item in log["action_order"] if item["player"] == "p1"
    ]
    assert p1_actions[0]["skill"] == "jab"
    cooldowns = arbiter.pet_state("m1", "p1", "a1")["cooldowns"]
    assert cooldowns["big"] >= 1


def test_ai_turn_limit_finishes_match():
    arbiter = new_arbiter(max_turns=3)
    start(arbiter, [pet("a1")], [pet("b1")])
    for turn in (1, 2, 3):
        commit(arbiter, "m1", turn)
        arbiter.resolve("m1", turn)
    state = arbiter.state("m1")
    assert state["finished"] is True
    assert state["result"] == "draw_timeout"
    extra = arbiter.resolve("m1", 4)
    assert extra["status"] == "match_finished"


def test_reconnect_state_and_snapshot_history():
    arbiter = new_arbiter()
    start(arbiter, [pet("a1")], [pet("b1")])
    commit(arbiter, "m1", 1)
    arbiter.resolve("m1", 1)
    commit(arbiter, "m1", 2)
    arbiter.resolve("m1", 2)
    state = arbiter.state("m1")
    assert state["turn"] == 3
    first = arbiter.snapshot_at("m1", 1)
    second = arbiter.snapshot_at("m1", 2)
    assert first["turn"] == 1
    assert first["hp"] == arbiter.turn_log("m1", 1)["hp"]
    assert second["hp"] != first["hp"]
    assert state["hp"] == second["hp"]


def test_restart_restores_hp_energy_cooldowns_and_buffs():
    arbiter = new_arbiter()
    pets_a = [
        pet(
            "a1",
            skills={
                "big": skill(power=30, cooldown=2, energy=10),
                "jab": skill(power=5),
            },
        )
    ]
    pets_b = [pet("b1", skills={"jab": skill(power=5)})]
    start(arbiter, pets_a, pets_b)
    arbiter.submit("m1", "p1", 1, {"type": "skill", "skill": "big"})
    arbiter.submit("m1", "p2", 1, {"type": "skill", "skill": "jab"})
    arbiter.resolve("m1", 1)
    state = arbiter.export_state()
    restarted = new_arbiter()
    restarted.load_state(state)
    pet_state = restarted.pet_state("m1", "p1", "a1")
    assert pet_state["hp"] == 95
    assert pet_state["energy"] == 90
    assert pet_state["cooldowns"]["big"] == 2
    assert restarted.state("m1")["turn"] == 2
    restarted.submit("m1", "p2", 2, {"type": "skill", "skill": "jab"})
    restarted.submit("m1", "p1", 2, {"type": "skill", "skill": "jab"})
    result = restarted.resolve("m1", 2)
    assert result["status"] == "resolved"
    assert restarted.state("m1")["turn"] == 3


def test_restart_does_not_auto_lose_active_match():
    arbiter = new_arbiter()
    start(arbiter, [pet("a1")], [pet("b1")])
    commit(arbiter, "m1", 1)
    arbiter.resolve("m1", 1)
    state = arbiter.export_state()
    restarted = new_arbiter()
    restarted.load_state(state)
    recovered = restarted.state("m1")
    assert recovered["finished"] is False
    assert recovered["result"] is None
    waiting = restarted.resolve("m1", 2)
    assert waiting["status"] == "waiting"
    assert restarted.rating("p1") == 1000
    assert restarted.rating("p2") == 1000


def test_turn_log_records_seed_commands_and_order():
    arbiter = new_arbiter(seed=1234)
    pets_a = [pet("a1", skills={"slash": skill(power=20, priority=1)})]
    pets_b = [pet("b1", skills={"slash": skill(power=20, priority=1)})]
    start(arbiter, pets_a, pets_b)
    commit(arbiter, "m1", 1, "slash")
    arbiter.resolve("m1", 1)
    log = arbiter.turn_log("m1", 1)
    assert log["seed"] == 1234
    assert log["commands"]["p1"]["skill"] == "slash"
    assert log["commands"]["p2"]["skill"] == "slash"
    assert len(log["action_order"]) == 2
    assert log["action_order"][0]["action"] == "skill"
    assert set(log["hp"]) == {"p1", "p2"}


def test_concurrent_submissions_both_accepted_and_resolve_once():
    arbiter = new_arbiter()
    start(arbiter, [pet("a1")], [pet("b1")])
    barrier = threading.Barrier(2)
    results = []
    results_lock = threading.Lock()

    def call(player):
        barrier.wait()
        result = arbiter.submit(
            "m1", player, 1, {"type": "skill", "skill": "jab"}
        )
        with results_lock:
            results.append(result)

    threads = [
        threading.Thread(target=call, args=("p1",)),
        threading.Thread(target=call, args=("p2",)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)

    assert [item["status"] for item in results] == ["accepted", "accepted"]
    resolved = arbiter.resolve("m1", 1)
    assert resolved["status"] == "resolved"
    assert arbiter.state("m1")["turn"] == 2
