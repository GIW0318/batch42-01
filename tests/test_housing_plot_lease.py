import threading

import pytest

import housing_plot_lease as hpl


def new_house(**kwargs):
    return hpl.HousingPlotLease(**kwargs)


def place_all(gate, plot, actor, sizes, slots):
    furniture_ids = []
    for index, (size, slot) in enumerate(zip(sizes, slots)):
        furniture_id = "f{}".format(index + 1)
        gate.add_furniture(furniture_id, actor, size)
        furniture_ids.append(furniture_id)
        gate.place(plot, actor, furniture_id, slot)
    return furniture_ids


def test_concurrent_place_in_same_slot_keeps_one_copy():
    gate = new_house()
    gate.create_plot("home", "alice", 1000)
    gate.add_furniture("f1", "alice", (1, 1))
    gate.add_furniture("f2", "alice", (1, 1))
    barrier = threading.Barrier(2)
    results = []
    errors = []
    results_lock = threading.Lock()

    def call(furniture_id):
        barrier.wait()
        try:
            outcome = gate.place("home", "alice", furniture_id, (0, 0))
            with results_lock:
                results.append(outcome)
        except hpl.SlotOccupiedError:
            with results_lock:
                errors.append("occupied")

    threads = [
        threading.Thread(target=call, args=("f1",)),
        threading.Thread(target=call, args=("f2",)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)

    assert len(results) == 1
    assert results[0]["status"] == "placed"
    assert errors == ["occupied"]
    house = gate.house_furniture("home")
    assert len(house) == 1
    loser = "f2" if house == ["f1"] else "f1"
    assert gate.furniture_location(loser) == ("bag", "alice")


def test_rotate_recomputes_footprint_and_keeps_old_on_collision():
    gate = new_house()
    gate.create_plot("home", "alice", 1000)
    gate.add_furniture("f1", "alice", (1, 2))
    gate.add_furniture("f2", "alice", (1, 1))
    gate.place("home", "alice", "f1", (0, 0), rotation=0)
    gate.place("home", "alice", "f2", (1, 0))
    assert gate.furniture_slots("f1") == [(0, 0), (0, 1)]
    with pytest.raises(hpl.SlotOccupiedError):
        gate.rotate("home", "alice", "f1", 90)
    assert gate.furniture_rotation("f1") == 0
    assert gate.furniture_slots("f1") == [(0, 0), (0, 1)]
    gate.move("home", "alice", "f2", (5, 5), expected_slot=(1, 0))
    rotated = gate.rotate("home", "alice", "f1", 90)
    assert rotated["status"] == "rotated"
    assert gate.furniture_rotation("f1") == 90
    assert gate.furniture_slots("f1") == [(0, 0), (1, 0)]


def test_revoke_access_blocks_old_teleport_immediately():
    gate = new_house()
    gate.create_plot("home", "alice", 1000, visitor_access=True)
    gate.grant_access("home", "alice", "bob")
    assert gate.enter("home", "bob", teleport="tp-old")["status"] == "entered"
    gate.revoke_access("home", "alice", "bob")
    with pytest.raises(hpl.PermissionDeniedError):
        gate.enter("home", "bob", teleport="tp-old")
    assert gate.enter("home", "alice")["status"] == "entered"


def test_renew_extends_expiry_and_charges_once():
    gate = new_house()
    gate.set_wallet("alice", 1000)
    gate.create_plot("home", "alice", 1000)
    result = gate.renew("home", "alice", now=940, days=30, rent=100)
    assert result["status"] == "renewed"
    assert result["lease_end"] == 1000 + 30 * 86400
    assert gate.wallet("alice") == 900
    assert gate.lease_status("home", 1001)["expired"] is False


def test_renew_with_insufficient_funds_changes_nothing():
    gate = new_house()
    gate.set_wallet("alice", 50)
    gate.create_plot("home", "alice", 1000)
    with pytest.raises(hpl.PaymentError):
        gate.renew("home", "alice", now=940, days=30, rent=100)
    assert gate.wallet("alice") == 50
    assert gate.lease_status("home", 1000)["lease_end"] == 1000


def test_move_out_excess_goes_to_storage_and_all_items_accounted():
    gate = new_house(mail_attachment_limit=2)
    gate.create_plot("home", "alice", 1000)
    ids = place_all(
        gate,
        "home",
        "alice",
        [(1, 1)] * 5,
        [(0, 0), (2, 0), (4, 0), (6, 0), (8, 0)],
    )
    result = gate.start_move_out("home", "alice")
    assert result["status"] == "completed"
    mailed = gate.mailbox("alice")
    stored = gate.storage("home")
    assert len(mailed) == 4
    assert len(stored) == 1
    assert sorted(mailed + stored) == sorted(ids)
    assert gate.house_furniture("home") == []


def test_move_out_rolls_back_atomically_on_mail_failure():
    gate = new_house(mail_attachment_limit=2)
    gate.create_plot("home", "alice", 1000)
    ids = place_all(
        gate,
        "home",
        "alice",
        [(1, 1)] * 5,
        [(0, 0), (2, 0), (4, 0), (6, 0), (8, 0)],
    )
    result = gate.start_move_out("home", "alice", mail_fail_at=2)
    assert result["status"] == "rolled_back"
    assert gate.house_furniture("home") == sorted(ids)
    assert gate.mailbox("alice") == []
    assert gate.storage("home") == []
    for furniture_id in ids:
        assert gate.furniture_location(furniture_id) == ("house", "home")


def test_restart_resumes_crashed_move_without_duplicates():
    gate = new_house(mail_attachment_limit=2)
    gate.create_plot("home", "alice", 1000)
    ids = place_all(
        gate,
        "home",
        "alice",
        [(1, 1)] * 5,
        [(0, 0), (2, 0), (4, 0), (6, 0), (8, 0)],
    )
    started = gate.start_move_out("home", "alice", crash_after=2)
    assert started["status"] == "in_progress"
    assert len(gate.mailbox("alice")) == 2
    assert len(gate.house_furniture("home")) == 3
    state = gate.export_state()
    restarted = new_house(mail_attachment_limit=2)
    restarted.load_state(state)
    resumed = restarted.resume_move_out("home")
    assert resumed["status"] == "completed"
    assert restarted.house_furniture("home") == []
    combined = restarted.mailbox("alice") + restarted.storage("home")
    assert sorted(combined) == sorted(ids)
    assert len(set(combined)) == 5


def test_restart_keeps_placed_furniture_and_inventory_consistent():
    gate = new_house()
    gate.create_plot("home", "alice", 1000)
    gate.add_furniture("f1", "alice", (1, 1))
    gate.place("home", "alice", "f1", (0, 0))
    state = gate.export_state()
    restarted = new_house()
    restarted.load_state(state)
    assert restarted.furniture_location("f1") == ("house", "home")
    assert restarted.house_furniture("home") == ["f1"]
    assert restarted.furniture_slots("f1") == [(0, 0)]
    assert restarted.storage("home") == []


def test_foreign_template_is_rejected_atomically():
    gate = new_house()
    gate.create_plot("home", "alice", 1000)
    gate.add_furniture("f1", "alice", (1, 1))
    gate.add_furniture("f2", "bob", (1, 1))
    before = gate.house_furniture("home")
    template = [
        {"furniture_id": "f3", "blueprint": "chair"},
        {"furniture_id": "f2", "blueprint": "chair"},
    ]
    with pytest.raises(hpl.InvalidTemplateError):
        gate.import_template("home", "alice", template)
    assert gate.house_furniture("home") == before
    assert gate.furniture_location("f2") == ("bag", "bob")


def test_concurrent_roommate_move_keeps_single_copy():
    gate = new_house()
    gate.create_plot("home", "alice", 1000)
    gate.add_furniture("f1", "alice", (1, 1))
    gate.place("home", "alice", "f1", (0, 0))
    barrier = threading.Barrier(2)
    results = []
    errors = []
    results_lock = threading.Lock()

    def call(target):
        barrier.wait()
        try:
            outcome = gate.move(
                "home", "alice", "f1", target, expected_slot=(0, 0)
            )
            with results_lock:
                results.append(outcome)
        except hpl.StaleMoveError:
            with results_lock:
                errors.append("stale")

    threads = [
        threading.Thread(target=call, args=((3, 3),)),
        threading.Thread(target=call, args=((4, 4),)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)

    assert len(results) == 1
    assert len(errors) == 1
    assert gate.house_furniture("home") == ["f1"]
    assert len(gate.furniture_slots("f1")) == 1


def test_reclaim_writes_audit_with_lease_version_and_operator():
    gate = new_house()
    gate.create_plot("home", "alice", 1000, version=1)
    gate.add_furniture("f1", "alice", (1, 1))
    gate.place("home", "alice", "f1", (0, 0))
    result = gate.reclaim_expired("home", now=2000, operator="admin")
    assert result["reclaimed"] is True
    entries = gate.audit_log("home")
    assert len(entries) == 1
    assert entries[0]["lease_version"] == 1
    assert entries[0]["operator"] == "admin"
    assert gate.furniture_location("f1") == ("storage", "home")
    again = gate.reclaim_expired("home", now=2001, operator="admin")
    assert again["reclaimed"] is False
    assert len(gate.audit_log("home")) == 1


def test_lease_expiry_ignores_timezone_and_waits_for_lease_end():
    gate = new_house()
    gate.create_plot("utc", "alice", 1000, timezone="UTC")
    gate.create_plot("plus14", "alice", 1000, timezone="UTC+14")
    assert gate.lease_status("utc", 999)["expired"] is False
    assert gate.lease_status("plus14", 999)["expired"] is False
    early = gate.reclaim_expired("utc", now=999, operator="system")
    assert early["reclaimed"] is False
    assert gate.audit_log("utc") == []
    late_utc = gate.reclaim_expired("utc", now=1001, operator="system")
    late_plus14 = gate.reclaim_expired(
        "plus14", now=1001, operator="system"
    )
    assert late_utc["reclaimed"] is True
    assert late_plus14["reclaimed"] is True


def test_visitor_cannot_edit_public_area_without_permission():
    gate = new_house()
    gate.create_plot("home", "alice", 1000, visitor_access=True)
    gate.grant_access("home", "alice", "bob")
    gate.add_furniture("b1", "bob", (1, 1))
    with pytest.raises(hpl.PermissionDeniedError):
        gate.place("home", "bob", "b1", (1, 1))
    gate.set_permission("home", "alice", public_edit=True)
    placed = gate.place("home", "bob", "b1", (1, 1))
    assert placed["status"] == "placed"
    assert gate.furniture_location("b1") == ("house", "home")


def test_storage_and_house_never_duplicate_item():
    gate = new_house()
    gate.create_plot("home", "alice", 1000)
    gate.add_furniture("f1", "alice", (1, 1))
    gate.place("home", "alice", "f1", (0, 0))
    first = gate.store("home", "alice", "f1")
    assert first["status"] == "stored"
    assert gate.house_furniture("home") == []
    assert gate.storage("home") == ["f1"]
    assert gate.mailbox("alice") == []
    second = gate.store("home", "alice", "f1")
    assert second["status"] == "not_found"
    assert gate.storage("home") == ["f1"]
    assert gate.furniture_location("f1") == ("storage", "home")
