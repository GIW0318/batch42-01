import threading

import pytest

import rsvp_lsp_planner
from rsvp_lsp_planner import RsvpError, RsvpLspPlanner


def _planner():
    planner = RsvpLspPlanner()
    planner.add_link("A", "B", 100)
    planner.add_link("B", "C", 100)
    planner.add_link("A", "C", 100)
    return planner


def _ero(*nodes):
    return [{"node": node, "loose": False} for node in nodes]


def test_establish_reserves_bandwidth():
    planner = _planner()
    lsp = planner.establish("lsp1", _ero("A", "B"), bandwidth=40, in_label=100)
    assert lsp["label"] == 100
    assert planner.available("A", "B") == 60
    assert planner.lsp_count() == 1


def test_insufficient_bandwidth_atomic():
    planner = _planner()
    with pytest.raises(RsvpError) as exc:
        planner.establish("lsp1", _ero("A", "B"), bandwidth=150)
    assert exc.value.code == "insufficient-bandwidth"
    assert planner.available("A", "B") == 100
    assert planner.lsp("lsp1") is None


def test_duplicate_label_rejected():
    planner = _planner()
    planner.establish("lsp1", _ero("A", "B"), bandwidth=10, in_label=100)
    with pytest.raises(RsvpError) as exc:
        planner.establish("lsp2", _ero("A", "C"), bandwidth=10, in_label=100)
    assert exc.value.code == "label-in-use"
    assert planner.available("A", "C") == 100


def test_label_released_on_teardown():
    planner = _planner()
    planner.establish("lsp1", _ero("A", "B"), bandwidth=40, in_label=100)
    result = planner.teardown("lsp1")
    assert result["released_label"] == 100
    assert planner.available("A", "B") == 100
    planner.establish("lsp2", _ero("A", "B"), bandwidth=40, in_label=100)


def test_path_and_resv_hop_mismatch_rejected():
    planner = _planner()
    with pytest.raises(RsvpError) as exc:
        planner.establish("lsp1", _ero("A", "B"), bandwidth=10, resv_hops=["A"])
    assert exc.value.code == "hop-mismatch"
    assert planner.available("A", "B") == 100


def test_loose_hop_skips_missing_strict_link():
    planner = _planner()
    ero = [
        {"node": "A", "loose": False},
        {"node": "X", "loose": True},
        {"node": "C", "loose": False},
    ]
    lsp = planner.establish("lsp1", ero, bandwidth=10)
    assert lsp["path"] == ["A", "B", "C"]


def test_strict_hop_missing_link_rejected():
    planner = _planner()
    ero = [
        {"node": "A", "loose": False},
        {"node": "X", "loose": False},
        {"node": "C", "loose": False},
    ]
    with pytest.raises(RsvpError) as exc:
        planner.establish("lsp1", ero, bandwidth=10)
    assert exc.value.code == "no-route"


def test_admin_group_requires_all_bits():
    planner = RsvpLspPlanner()
    planner.add_link("A", "B", 100, admin_group=0b0011)
    planner.add_link("A", "C", 100, admin_group=0b0101)
    with pytest.raises(RsvpError) as exc:
        planner.establish("lsp1", _ero("A", "B"), bandwidth=10, admin_group=0b0101)
    assert exc.value.code == "admin-group-mismatch"
    assert planner.establish("lsp2", _ero("A", "C"), bandwidth=10,
                             admin_group=0b0101)["label"]


def test_preemption_order_uses_numeric_priority():
    planner = _planner()
    planner.establish("lsp-low", _ero("A", "B"), bandwidth=10, priority=7)
    planner.establish("lsp-high", _ero("A", "C"), bandwidth=10, priority=1)
    assert planner.preemption_order() == ["lsp-high", "lsp-low"]


def test_teardown_notifies_downstream():
    planner = _planner()
    planner.establish("lsp1", _ero("A", "B", "C"), bandwidth=10, in_label=100)
    result = planner.teardown("lsp1")
    assert result["downstream_notified"] == ["B", "C"]
    assert planner.available("A", "B") == 100
    assert planner.available("B", "C") == 100


def test_path_error_cleans_half_state_then_reestablish():
    planner = _planner()
    planner.establish("lsp1", _ero("A", "B"), bandwidth=40, in_label=100)
    assert planner.path_error("lsp1", "B") is True
    assert planner.lsp("lsp1") is None
    assert planner.available("A", "B") == 100
    assert planner.establish("lsp1", _ero("A", "B"), bandwidth=40,
                             in_label=100)["label"] == 100


def test_refresh_deadlines_differ_and_persist():
    planner = _planner()
    planner.establish("lsp1", _ero("A", "B"), bandwidth=10)
    planner.advance(10)
    planner.establish("lsp2", _ero("A", "C"), bandwidth=10)
    assert planner.refresh_deadline("lsp1") == 30
    assert planner.refresh_deadline("lsp2") == 40
    snapshot = planner.snapshot()
    restored = RsvpLspPlanner()
    restored.restore(snapshot)
    assert restored.refresh_deadline("lsp1") == 30
    assert restored.refresh_deadline("lsp2") == 40
    restored.advance(25)
    assert restored.expired() == ["lsp1"]


def test_concurrent_establish_single_lsp():
    planner = _planner()
    barrier = threading.Barrier(2)
    results = []
    errors = []
    lock = threading.Lock()

    def worker():
        barrier.wait()
        try:
            outcome = planner.establish("lsp1", _ero("A", "B"), bandwidth=10)
            with lock:
                results.append(outcome)
        except RsvpError as exc:
            with lock:
                errors.append(exc.code)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(results) + len(errors) == 2
    assert planner.lsp_count() == 1
    assert planner.available("A", "B") == 90


def test_reconcile_detects_hop_mismatch():
    planner = _planner()
    planner.establish("lsp1", _ero("A", "B"), bandwidth=10)
    assert planner.reconcile("lsp1", _ero("A", "B")) is True
    assert planner.reconcile("lsp1", _ero("A", "C")) is False


def test_restart_restores_state_and_keeps_label_reserved():
    planner = _planner()
    planner.establish("lsp1", _ero("A", "B"), bandwidth=40, in_label=100)
    snapshot = planner.snapshot()
    restored = RsvpLspPlanner()
    restored.restore(snapshot)
    assert restored.lsp("lsp1")["label"] == 100
    assert restored.available("A", "B") == 60
    with pytest.raises(RsvpError) as exc:
        restored.establish("lsp2", _ero("A", "C"), bandwidth=10, in_label=100)
    assert exc.value.code == "label-in-use"
