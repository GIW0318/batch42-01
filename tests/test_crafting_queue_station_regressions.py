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


def test_concurrent_cancel_refunds_once():
    gate = new_gate()
    gate.enqueue("o1", "alice", "long")
    gate.advance(30)
    barrier = threading.Barrier(2)
    results = []
    results_lock = threading.Lock()

    def call():
        barrier.wait()
        result = gate.cancel("o1")
        with results_lock:
            results.append(result)

    threads = [threading.Thread(target=call) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)

    assert [item["status"] for item in results] == ["cancelled", "cancelled"]
    assert gate.inventory("alice")["iron"] == 98
    assert len(gate.refund_log("o1")) == 1
    assert gate.queue("alice") == []


def test_reorder_waiting_order_never_recharges_or_resets_craft():
    gate = new_gate()
    for order_id in ("o1", "o2", "o3"):
        gate.enqueue(order_id, "alice", "long")
    gate.advance(10)
    before = gate.inventory("alice")

    assert gate.reorder("o3", 1)["status"] == "reordered"
    assert order_ids(gate) == ["o1", "o3", "o2"]

    blocked = gate.reorder("o1", 2)
    assert blocked["status"] == "in_progress"
    assert gate.progress("o1")["remaining"] == 50
    assert gate.inventory("alice") == before


def test_restart_uses_snapshot_and_does_not_duplicate_claim():
    gate = new_gate()
    gate.enqueue("o1", "alice", "sword")
    gate.advance(20)
    snapshot = gate.export_state()

    restarted = new_gate()
    restarted.load_state(snapshot)
    restarted.add_recipe(
        "sword",
        version=2,
        materials={"iron": 1},
        seconds=60,
        output={"sword_v2": 1},
        durability_cost=5,
    )

    assert restarted.progress("o1")["recipe_version"] == 1
    assert restarted.progress("o1")["remaining"] == 40
    assert restarted.inventory("alice")["iron"] == 98

    restarted.advance(40)
    assert restarted.claim("o1")["items"] == {"sword": 1}
    assert restarted.claim("o1")["status"] == "already_claimed"
    assert restarted.inventory("alice")["sword"] == 1


def test_expiry_stays_with_old_order_when_recipe_is_added_again():
    gate = new_gate()
    gate.enqueue("old", "alice", "event")
    gate.advance(30)
    gate.expire_recipe("event")
    gate.add_recipe(
        "event",
        version=2,
        materials={"iron": 1},
        seconds=30,
        output={"token": 1},
        durability_cost=2,
    )

    result = gate.advance(0)
    assert result["failed"] == ["old"]
    assert gate.inventory("alice")["iron"] == 100
    assert gate.refund_log("old")[0]["refund"] == {"iron": 4}
    assert gate.refund_log("old")[0]["recipe_version"] == 1

    gate.enqueue("new", "alice", "event")
    assert gate.inventory("alice")["iron"] == 99
    assert gate.progress("new")["recipe_version"] == 2


def test_quantity_scales_investment_duration_output_and_refund():
    gate = new_gate()
    gate.enqueue("o1", "alice", "long", quantity=2)
    assert gate.inventory("alice")["iron"] == 92
    assert gate.progress("o1")["remaining"] == 120

    gate.advance(30)
    cancelled = gate.cancel("o1")
    assert cancelled["refund"] == {"iron": 6}
    assert gate.inventory("alice")["iron"] == 98

    gate.enqueue("o2", "alice", "long", quantity=2)
    gate.advance(120)
    assert gate.claim("o2")["items"] == {"bar": 2}


def test_broken_station_rejects_before_touching_materials_or_queue():
    gate = new_gate(capacity=1)
    gate.set_durability(0)
    with pytest.raises(cqs.StationBrokenError):
        gate.enqueue("o1", "alice", "long")
    assert gate.inventory("alice") == {"iron": 100, "wood": 100}
    assert order_ids(gate) == []
