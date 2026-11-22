import copy
import threading

import pytest

import crafting_queue_station as cqs


RECIPES = {
    "sword": {
        "version": 1,
        "materials": {"iron": 2, "wood": 1},
        "seconds": 60,
        "output": {"sword": 1},
        "durability_cost": 10,
    },
    "long": {
        "version": 1,
        "materials": {"iron": 4},
        "seconds": 60,
        "output": {"bar": 1},
        "durability_cost": 5,
    },
    "event": {
        "version": 1,
        "materials": {"iron": 4},
        "seconds": 60,
        "output": {"token": 1},
        "durability_cost": 5,
    },
}


def new_gate(**kwargs):
    kwargs.setdefault("recipes", copy.deepcopy(RECIPES))
    kwargs.setdefault(
        "inventory",
        {
            "alice": {"iron": 100, "wood": 100},
            "bob": {"iron": 10, "wood": 10},
        },
    )
    return cqs.CraftingQueueStation(**kwargs)


def order_ids(gate, character="alice"):
    return [item["order_id"] for item in gate.queue(character)]


def test_enqueue_consumes_materials_and_occupies_queue():
    gate = new_gate()
    result = gate.enqueue("o1", "alice", "sword")
    assert result["status"] == "queued"
    assert result["position"] == 0
    assert result["remaining"] == 60
    assert gate.inventory("alice") == {"iron": 98, "wood": 99}
    assert order_ids(gate) == ["o1"]
    assert gate.progress("o1")["status"] == "waiting"


def test_cancel_waiting_order_refunds_only_that_order():
    gate = new_gate()
    gate.enqueue("o1", "alice", "sword")
    gate.enqueue("o2", "alice", "sword")
    gate.enqueue("o3", "alice", "sword")
    result = gate.cancel("o3")
    assert result["status"] == "cancelled"
    assert result["refund"] == {"iron": 2, "wood": 1}
    assert gate.inventory("alice") == {"iron": 96, "wood": 98}
    assert order_ids(gate) == ["o1", "o2"]
    assert gate.progress("o3")["status"] == "cancelled"


def test_cancel_in_progress_refunds_unconsumed_exactly():
    gate = new_gate()
    gate.enqueue("o1", "alice", "long")
    gate.advance(30)
    assert gate.progress("o1")["remaining"] == 30
    result = gate.cancel("o1")
    assert result["refund"] == {"iron": 2}
    assert gate.inventory("alice")["iron"] == 98
    assert gate.queue("alice") == []


def test_refund_log_matches_cancelled_order_once():
    gate = new_gate()
    gate.enqueue("o1", "alice", "long")
    gate.enqueue("o2", "alice", "long")
    gate.advance(15)
    refund = gate.cancel("o2")["refund"]
    entries = gate.refund_log("o2")
    assert len(entries) == 1
    assert entries[0]["order_id"] == "o2"
    assert entries[0]["refund"] == refund
    assert entries[0]["recipe_version"] == 1
    assert gate.refund_log("o1") == []


def test_advance_completes_in_order_and_eta_shrinks():
    gate = new_gate()
    gate.enqueue("o1", "alice", "long")
    gate.enqueue("o2", "alice", "long")
    gate.enqueue("o3", "alice", "long")
    result = gate.advance(150)
    assert result["completed"] == ["o1", "o2"]
    assert gate.progress("o1")["status"] == "ready"
    assert gate.progress("o2")["status"] == "ready"
    assert gate.progress("o3")["remaining"] == 30
    assert gate.progress("o3")["remaining"] < 60


def test_concurrent_claim_succeeds_once():
    gate = new_gate()
    gate.enqueue("o1", "alice", "long")
    gate.advance(60)
    assert gate.progress("o1")["status"] == "ready"
    barrier = threading.Barrier(2)
    results = []
    results_lock = threading.Lock()

    def call():
        barrier.wait()
        result = gate.claim("o1")
        with results_lock:
            results.append(result)

    threads = [threading.Thread(target=call) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)

    assert sorted(item["status"] for item in results) == [
        "already_claimed",
        "claimed",
    ]
    assert gate.inventory("alice")["bar"] == 1


def test_in_progress_order_cannot_be_reordered_or_recharged():
    gate = new_gate()
    gate.enqueue("o1", "alice", "long")
    gate.enqueue("o2", "alice", "long")
    gate.advance(10)
    before = gate.inventory("alice")
    result = gate.reorder("o1", 1)
    assert result["status"] == "in_progress"
    assert order_ids(gate) == ["o1", "o2"]
    assert gate.inventory("alice") == before
    assert gate.progress("o1")["remaining"] == 50


def test_reset_profession_fails_order_and_refunds():
    gate = new_gate()
    gate.enqueue("o1", "alice", "long")
    gate.advance(30)
    result = gate.reset_profession("alice")
    assert result["failed"] == ["o1"]
    assert result["refunded"] == {"iron": 2}
    assert gate.inventory("alice")["iron"] == 98
    assert "bar" not in gate.inventory("alice")
    assert gate.queue("alice") == []
    assert gate.progress("o1")["status"] == "failed"


def test_station_broken_rejects_new_order():
    gate = new_gate()
    gate.set_durability(0)
    with pytest.raises(cqs.StationBrokenError):
        gate.enqueue("o1", "alice", "long")
    assert gate.inventory("alice") == {"iron": 100, "wood": 100}
    assert gate.queue("alice") == []


def test_queue_full_rejects_without_deducting():
    gate = new_gate(capacity=1)
    gate.enqueue("o1", "alice", "long")
    before = gate.inventory("alice")
    with pytest.raises(cqs.QueueFullError):
        gate.enqueue("o2", "alice", "long")
    assert gate.inventory("alice") == before
    assert order_ids(gate) == ["o1"]


def test_restart_restores_remaining_seconds_and_claim_once():
    gate = new_gate()
    gate.enqueue("o1", "alice", "long")
    gate.advance(20)
    state = gate.export_state()
    restarted = new_gate()
    restarted.load_state(state)
    assert restarted.progress("o1")["remaining"] == 40
    assert restarted.inventory("alice")["iron"] == 96
    restarted.advance(40)
    claimed = restarted.claim("o1")
    assert claimed["status"] == "claimed"
    assert claimed["items"] == {"bar": 1}
    assert restarted.inventory("alice")["bar"] == 1
    again = restarted.claim("o1")
    assert again["status"] == "already_claimed"
    assert restarted.inventory("alice")["bar"] == 1


def test_recipe_version_is_pinned_to_order():
    gate = new_gate()
    gate.enqueue("o1", "alice", "sword")
    gate.add_recipe(
        "sword",
        version=2,
        materials={"iron": 1},
        seconds=60,
        output={"sword_v2": 1},
        durability_cost=5,
    )
    cancelled = gate.cancel("o1")
    assert cancelled["refund"] == {"iron": 2, "wood": 1}
    assert gate.inventory("alice") == {"iron": 100, "wood": 100}
    gate.enqueue("o2", "alice", "sword")
    assert gate.inventory("alice") == {"iron": 99, "wood": 100}
    gate.advance(60)
    claimed = gate.claim("o2")
    assert claimed["status"] == "claimed"
    assert claimed["items"] == {"sword_v2": 1}
    assert gate.progress("o2")["recipe_version"] == 2


def test_expired_event_recipe_refunds_full_original_cost():
    gate = new_gate()
    gate.enqueue("o1", "alice", "event")
    gate.enqueue("o2", "alice", "event")
    assert gate.inventory("alice")["iron"] == 92
    gate.expire_recipe("event")
    result = gate.advance(0)
    assert sorted(result["failed"]) == ["o1", "o2"]
    assert gate.inventory("alice")["iron"] == 100
    assert "token" not in gate.inventory("alice")
    assert gate.refund_log("o1")[0]["refund"] == {"iron": 4}
    assert gate.refund_log("o2")[0]["refund"] == {"iron": 4}


def test_per_character_queue_isolation():
    gate = new_gate()
    gate.enqueue("a1", "alice", "sword")
    gate.enqueue("b1", "bob", "long")
    assert order_ids(gate, "alice") == ["a1"]
    assert order_ids(gate, "bob") == ["b1"]
    assert gate.inventory("alice") == {"iron": 98, "wood": 99}
    assert gate.inventory("bob") == {"iron": 6, "wood": 10}


def test_concurrent_enqueue_for_last_slot_only_one_wins():
    gate = new_gate(capacity=2)
    gate.enqueue("seed", "alice", "long")
    barrier = threading.Barrier(2)
    results = []
    errors = []
    results_lock = threading.Lock()

    def call(order_id, recipe_id):
        barrier.wait()
        try:
            outcome = gate.enqueue(order_id, "alice", recipe_id)
            with results_lock:
                results.append(outcome)
        except cqs.QueueFullError:
            with results_lock:
                errors.append("full")

    threads = [
        threading.Thread(target=call, args=("o1", "long")),
        threading.Thread(target=call, args=("o2", "long")),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)

    assert len(results) == 1
    assert results[0]["status"] == "queued"
    assert errors == ["full"]
    assert len(gate.queue("alice")) == 2
    assert gate.inventory("alice")["iron"] == 92
