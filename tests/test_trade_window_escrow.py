import threading

import pytest

import trade_window_escrow as twe


KNOWN = {"sword", "shield", "ore"}


def default_bags():
    return {
        "alice": {
            "gold": 500000,
            "items": {
                "sword": {"count": 1, "level": 0},
                "ore": {"count": 10, "level": 0},
            },
        },
        "bob": {
            "gold": 200000,
            "items": {
                "shield": {"count": 1, "level": 0},
                "ore": {"count": 2, "level": 0},
            },
        },
    }


def new_gate(**kwargs):
    kwargs.setdefault("bags", default_bags())
    kwargs.setdefault("known_items", set(KNOWN))
    return twe.TradeWindowEscrow(**kwargs)


def prepare(gate, trade_id="t1", gold=100000):
    gate.open(trade_id, "alice", "bob")
    gate.offer(trade_id, "a", {"sword": 1}, gold)
    gate.offer(trade_id, "b", {"shield": 1}, 0)
    gate.lock(trade_id, "a")
    gate.lock(trade_id, "b")


def test_offer_is_frozen_after_lock():
    gate = new_gate()
    gate.open("t1", "alice", "bob")
    gate.offer("t1", "a", {"sword": 1}, 100000)
    assert gate.lock("t1", "a")["status"] == "locked"
    result = gate.offer("t1", "a", {"sword": 1}, 1000000)
    assert result["status"] == "locked"
    assert gate.escrow("t1")["a"]["gold"] == 100000
    assert gate.inventory("alice")["gold"] == 400000


def test_settlement_swaps_items_and_gold_exactly():
    gate = new_gate()
    prepare(gate)
    assert gate.confirm("t1", "a")["status"] == "confirmed"
    assert gate.confirm("t1", "b")["status"] == "settled"
    alice = gate.inventory("alice")
    bob = gate.inventory("bob")
    assert alice["gold"] == 400000
    assert bob["gold"] == 300000
    assert alice["items"]["shield"]["count"] == 1
    assert "sword" not in alice["items"]
    assert bob["items"]["sword"]["count"] == 1
    assert "shield" not in bob["items"]
    assert gate.escrow("t1")["a"] == {"items": {}, "gold": 0}
    assert gate.escrow("t1")["b"] == {"items": {}, "gold": 0}


def test_duplicate_confirm_returns_already_settled_and_moves_nothing():
    gate = new_gate()
    prepare(gate)
    gate.confirm("t1", "a")
    gate.confirm("t1", "b")
    alice_before = gate.inventory("alice")
    bob_before = gate.inventory("bob")
    repeat = gate.confirm("t1", "b")
    assert repeat["status"] == "trade_already_settled"
    assert repeat["settled"] is True
    assert gate.inventory("alice") == alice_before
    assert gate.inventory("bob") == bob_before


def test_concurrent_confirm_settles_once():
    gate = new_gate()
    prepare(gate)
    gate.confirm("t1", "a")
    barrier = threading.Barrier(2)
    results = []
    results_lock = threading.Lock()

    def call():
        barrier.wait()
        result = gate.confirm("t1", "b")
        with results_lock:
            results.append(result)

    threads = [threading.Thread(target=call) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)

    assert sorted(item["status"] for item in results) == [
        "settled",
        "trade_already_settled",
    ]
    assert gate.inventory("alice")["gold"] == 400000
    assert gate.inventory("bob")["gold"] == 300000
    assert gate.escrow("t1")["a"] == {"items": {}, "gold": 0}


def test_gold_above_cap_raises_and_changes_nothing():
    gate = new_gate()
    gate.open("t1", "alice", "bob")
    before = gate.inventory("alice")
    with pytest.raises(twe.CurrencyOverflowError):
        gate.offer("t1", "a", {}, 1000001)
    assert gate.inventory("alice") == before
    assert gate.escrow("t1")["a"] == {"items": {}, "gold": 0}


def test_negative_gold_and_invalid_item_counts_are_rejected():
    gate = new_gate()
    gate.open("t1", "alice", "bob")
    with pytest.raises(twe.CurrencyOverflowError):
        gate.offer("t1", "a", {"sword": 1}, -1)
    with pytest.raises(twe.InvalidOfferError):
        gate.offer("t1", "a", {"sword": 0}, 0)
    with pytest.raises(twe.InvalidOfferError):
        gate.offer("t1", "a", {"sword": 2}, 0)
    assert gate.inventory("alice")["items"]["sword"]["count"] == 1
    assert gate.escrow("t1")["a"]["gold"] == 0


def test_receiver_full_bag_fails_whole_trade_and_refunds():
    gate = new_gate(bag_slots=2)
    before = {name: gate.inventory(name) for name in ("alice", "bob")}
    gate.open("t1", "alice", "bob")
    gate.offer("t1", "a", {"sword": 1, "ore": 2}, 0)
    gate.lock("t1", "a")
    assert gate.confirm("t1", "a")["status"] == "confirmed"
    with pytest.raises(twe.InventoryFullError):
        gate.confirm("t1", "b")
    assert gate.state("t1") == "failed"
    assert gate.escrow("t1")["a"] == {"items": {}, "gold": 0}
    assert gate.inventory("alice") == before["alice"]
    assert gate.inventory("bob") == before["bob"]


def test_item_change_during_trade_invalidates_and_clears_locks():
    gate = new_gate()
    prepare(gate)
    assert gate.inventory("alice").get("sword") is None
    changed = gate.apply_item_change("alice", "sword", level=1)
    assert "t1" in changed["invalidated"]
    assert gate.state("t1") == "cancelled"
    assert gate.escrow("t1")["a"] == {"items": {}, "gold": 0}
    sword = gate.inventory("alice")["items"]["sword"]
    assert sword["count"] == 1
    assert sword["level"] == 1
    assert gate.confirm("t1", "a")["status"] == "trade_cancelled"


def test_cancel_returns_escrow_and_clears_it():
    gate = new_gate()
    before = {name: gate.inventory(name) for name in ("alice", "bob")}
    prepare(gate)
    result = gate.cancel("t1", "a")
    assert result["status"] == "cancelled"
    assert gate.state("t1") == "cancelled"
    assert gate.escrow("t1")["a"] == {"items": {}, "gold": 0}
    assert gate.escrow("t1")["b"] == {"items": {}, "gold": 0}
    assert gate.inventory("alice") == before["alice"]
    assert gate.inventory("bob") == before["bob"]


def test_two_trades_cannot_lock_same_ore_twice():
    gate = new_gate()
    gate.open("t1", "alice", "bob")
    gate.offer("t1", "a", {"ore": 6}, 0)
    assert gate.lock("t1", "a")["status"] == "locked"
    gate.open("t2", "alice", "bob")
    gate.offer("t2", "a", {"ore": 6}, 0)
    second = gate.lock("t2", "a")
    assert second["status"] == "insufficient"
    assert second["reason"] == "not enough items"
    assert gate.inventory("alice")["items"]["ore"]["count"] == 4
    assert gate.escrow("t1")["a"]["items"]["ore"]["count"] == 6
    assert gate.inventory("bob")["items"]["ore"]["count"] == 2
    assert gate.escrow("t2")["a"]["items"] == {}


def test_disconnect_returns_escrow_and_blocks_later_settlement():
    gate = new_gate()
    before = {name: gate.inventory(name) for name in ("alice", "bob")}
    prepare(gate)
    result = gate.disconnect("t1", "b")
    assert result["status"] == "cancelled"
    assert result["reason"] == "peer disconnected"
    assert gate.inventory("alice") == before["alice"]
    assert gate.inventory("bob") == before["bob"]
    assert gate.confirm("t1", "a")["status"] == "trade_cancelled"


def test_reopening_settled_trade_cannot_settle_again():
    gate = new_gate()
    prepare(gate)
    gate.confirm("t1", "a")
    gate.confirm("t1", "b")
    alice_before = gate.inventory("alice")
    bob_before = gate.inventory("bob")
    reopened = gate.reopen("t1")
    assert reopened["status"] == "trade_closed"
    repeat = gate.confirm("t1", "a")
    assert repeat["status"] == "trade_already_settled"
    assert gate.inventory("alice") == alice_before
    assert gate.inventory("bob") == bob_before


def test_unknown_item_is_rejected_without_removal():
    gate = new_gate()
    gate.open("t1", "alice", "bob")
    result = gate.offer("t1", "a", {"relic": 1}, 0)
    assert result["status"] == "unknown_item"
    assert "relic" in result["reason"]
    assert gate.inventory("alice")["items"]["sword"]["count"] == 1
    assert gate.escrow("t1")["a"] == {"items": {}, "gold": 0}


def test_restart_preserves_locked_trade_and_escrow():
    gate = new_gate()
    before = {name: gate.inventory(name) for name in ("alice", "bob")}
    prepare(gate)
    state = gate.export_state()
    restarted = twe.TradeWindowEscrow(bags={}, known_items=set(KNOWN))
    restarted.load_state(state)
    assert restarted.state("t1") == "locked"
    assert restarted.escrow("t1")["a"]["gold"] == 100000
    assert restarted.inventory("alice")["gold"] == 400000
    restarted.cancel("t1")
    assert restarted.inventory("alice") == before["alice"]
    assert restarted.inventory("bob") == before["bob"]


def test_restart_after_partial_confirm_completes_once():
    gate = new_gate()
    prepare(gate)
    gate.confirm("t1", "a")
    state = gate.export_state()
    restarted = twe.TradeWindowEscrow(bags={}, known_items=set(KNOWN))
    restarted.load_state(state)
    assert restarted.state("t1") == "pending_confirmation"
    result = restarted.confirm("t1", "b")
    assert result["status"] == "settled"
    alice = restarted.inventory("alice")
    bob = restarted.inventory("bob")
    assert alice["gold"] == 400000
    assert bob["gold"] == 300000
    assert alice["items"]["shield"]["count"] == 1
    assert bob["items"]["sword"]["count"] == 1
    assert restarted.confirm("t1", "b")["status"] == "trade_already_settled"
    assert restarted.inventory("alice") == alice
    assert restarted.inventory("bob") == bob
