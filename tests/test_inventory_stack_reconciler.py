import pytest

import inventory_stack_reconciler as inv


def make(**kwargs):
    return inv.StackReconciler(**kwargs)


def test_split_reduces_source_and_keeps_total():
    record = make()
    record.put("bag", 0, "potion", 10)
    assert record.split("op1", ("bag", 0), 4, ("bag", 1)) is True
    assert record.slot("bag", 0)["count"] == 6
    assert record.slot("bag", 1)["count"] == 4
    assert record.total("potion") == 10


def test_merging_full_stacks_splits_overflow_instead_of_losing_it():
    record = make()
    record.put("bag", 0, "arrow", 99, max_stack=99)
    record.put("bag", 1, "arrow", 99, max_stack=99)
    record.merge("op1", ("bag", 0), ("bag", 1))
    assert record.total("arrow") == 198
    for slot in record.slots("bag"):
        if slot["item_id"] == "arrow":
            assert slot["count"] <= 99


def test_organize_never_clears_bind_flag():
    record = make()
    record.put("bag", 0, "sword", 1, bind=True)
    record.put("bag", 1, "sword", 1, bind=False)
    record.organize("op1", "bag")
    assert record.total("sword") == 2
    flags = sorted(slot["bind"] for slot in record.slots("bag")
                   if slot["item_id"] == "sword")
    assert flags == [False, True]


def test_duplicate_operation_id_is_applied_once():
    record = make()
    record.put("bag", 0, "potion", 5)
    assert record.move("op1", ("bag", 0), ("bag", 1), count=2) is True
    assert record.move("op1", ("bag", 0), ("bag", 1), count=2) is False
    assert record.slot("bag", 0)["count"] == 3
    assert record.slot("bag", 1)["count"] == 2
    assert record.total("potion") == 5


def test_total_counts_bag_warehouse_mail_and_temp_together():
    record = make()
    record.put("bag", 0, "ore", 1)
    record.put("warehouse", 0, "ore", 2)
    record.put("mail", 0, "ore", 4)
    record.put("temp", 0, "ore", 8)
    assert record.total("ore") == 15


def test_mail_extraction_into_full_bag_keeps_attachment():
    record = make(bag_size=2)
    record.put("bag", 0, "ore", 1)
    record.put("bag", 1, "ore", 1)
    record.put("mail", 0, "gem", 1)
    with pytest.raises(inv.StackConflictError):
        record.extract_mail("op1", 0, "bag")
    assert record.slot("mail", 0)["count"] == 1
    assert record.total("gem", include_mail=True) == 1


def test_cross_page_merge_does_not_zero_the_second_page():
    record = make()
    record.put("warehouse", 50, "ore", 10)
    record.put("warehouse", 150, "ore", 10)
    record.merge("op1", ("warehouse", 50), ("warehouse", 150))
    assert record.total("ore") == 20
    counts = [slot["count"] for slot in record.slots("warehouse")
              if slot["item_id"] == "ore"]
    assert sum(counts) == 20


def test_organize_does_not_merge_same_name_items_with_different_durability():
    record = make()
    record.put("bag", 0, "blade", 1, durability=50)
    record.put("bag", 1, "blade", 1, durability=100)
    record.organize("op1", "bag")
    durabilities = sorted(slot["durability"] for slot in record.slots("bag")
                          if slot["item_id"] == "blade")
    assert durabilities == [50, 100]
    assert record.total("blade") == 2


def test_stale_slot_version_is_rejected():
    record = make()
    record.put("bag", 0, "ore", 5)
    old_version = record.slot("bag", 0)["version"]
    record.move("op1", ("bag", 0), ("bag", 1), count=1,
                expected_version=old_version)
    with pytest.raises(inv.StackConflictError):
        record.move("op2", ("bag", 0), ("bag", 1), count=1,
                    expected_version=old_version)


def test_restore_does_not_replay_a_committed_operation():
    record = make()
    record.put("bag", 0, "ore", 5)
    record.move("op1", ("bag", 0), ("bag", 1), count=2)
    saved = record.snapshot()
    restored = make()
    restored.restore(saved)
    assert restored.total("ore") == 5
    assert restored.move("op1", ("bag", 0), ("bag", 1), count=2) is False
    assert restored.total("ore") == 5


def test_interrupted_move_leaves_no_partial_state_and_can_resume():
    record = make()
    record.put("bag", 0, "ore", 5)
    with pytest.raises(inv.StackConflictError):
        record.move("op1", ("bag", 0), ("bag", 1), count=3, crash_midway=True)
    assert record.slot("bag", 0)["count"] == 5
    assert record.total("ore") == 5
    record.resume_pending()
    assert record.slot("bag", 1)["count"] == 3
    assert record.slot("bag", 0)["count"] == 2


def test_selling_partial_stack_charges_only_the_sold_count():
    record = make()
    record.put("bag", 0, "arrow", 10)
    price = record.sell("op1", "bag", 0, 3, unit_price=5)
    assert price == 15
    assert record.slot("bag", 0)["count"] == 7
    assert record.total("arrow") == 7


def test_bound_item_keeps_bind_when_moved_to_guild_bank():
    record = make()
    record.put("bag", 0, "relic", 1, bind=True)
    record.move("op1", ("bag", 0), ("guild", 0))
    assert record.slot("guild", 0)["bind"] is True


def test_bag_index_199_works_and_index_200_is_rejected():
    record = make(bag_size=200)
    assert record.put("bag", 199, "ore", 1)["count"] == 1
    with pytest.raises(inv.StackConflictError):
        record.put("bag", 200, "ore", 1)


def test_non_positive_quantities_are_rejected():
    record = make()
    record.put("bag", 0, "ore", 5)
    with pytest.raises(inv.StackConflictError):
        record.split("op1", ("bag", 0), 0, ("bag", 1))
    with pytest.raises(inv.StackConflictError):
        record.destroy("op2", ("bag", 0), -1)
    assert record.total("ore") == 5
