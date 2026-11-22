import threading

import pytest

import sae_commit_scheduler
from sae_commit_scheduler import SaeCommitScheduler, SaeError

SCALAR = b"s" * 32
ELEMENT = b"e" * 64


def _scheduler(**kwargs):
    options = dict(credentials={"sta1": "right"}, commit_timeout=30)
    options.update(kwargs)
    return SaeCommitScheduler(**options)


def _commit(sched, sta="sta1", password="right", group=19):
    token = sched.issue_token(sta)
    return sched.commit(sta, token, group=group, scalar=SCALAR,
                        element=ELEMENT, password=password)


def test_commit_without_token_rejected():
    sched = _scheduler()
    with pytest.raises(SaeError) as exc:
        sched.commit("sta1", "", scalar=SCALAR, element=ELEMENT, password="right")
    assert exc.value.code == "anti-clogging"
    assert sched.session_state("sta1") == "none"


def test_token_bound_to_sta():
    sched = _scheduler(credentials={"sta1": "right", "sta2": "right"})
    token = sched.issue_token("sta1")
    with pytest.raises(SaeError) as exc:
        sched.commit("sta2", token, scalar=SCALAR, element=ELEMENT, password="right")
    assert exc.value.code == "token-mismatch"


def test_commit_replay_idempotent():
    sched = _scheduler()
    token = sched.issue_token("sta1")
    first = sched.commit("sta1", token, scalar=SCALAR, element=ELEMENT,
                         password="right")
    second = sched.commit("sta1", token, scalar=SCALAR, element=ELEMENT,
                          password="right")
    assert first["session_id"] == second["session_id"]
    assert sched.session_state("sta1") == "committed"


def test_disallowed_group_rejected():
    sched = _scheduler()
    with pytest.raises(SaeError) as exc:
        _commit(sched, group=1)
    assert exc.value.code == "group-not-allowed"


def test_invalid_scalar_length_rejected():
    sched = _scheduler()
    token = sched.issue_token("sta1")
    with pytest.raises(SaeError) as exc:
        sched.commit("sta1", token, scalar=b"s" * 31, element=ELEMENT,
                     password="right")
    assert exc.value.code == "invalid-length"
    assert exc.value.field == "scalar"


def test_invalid_element_length_rejected():
    sched = _scheduler()
    token = sched.issue_token("sta1")
    with pytest.raises(SaeError) as exc:
        sched.commit("sta1", token, scalar=SCALAR, element=b"e" * 63,
                     password="right")
    assert exc.value.code == "invalid-length"
    assert exc.value.field == "element"


def test_confirm_mismatched_ids_rejected():
    sched = _scheduler()
    session = _commit(sched)
    with pytest.raises(SaeError) as exc:
        sched.confirm("sta1", session["recv_id"], session["send_id"])
    assert exc.value.code == "confirm-mismatch"


def test_key_install_atomic_with_state():
    sched = _scheduler()
    session = _commit(sched)
    with pytest.raises(SaeError):
        sched.confirm("sta1", "bad", "bad")
    assert sched.key_install_count("sta1") == 0
    assert sched.session_state("sta1") == "committed"
    result = sched.confirm("sta1", session["send_id"], session["recv_id"])
    assert result["state"] == "authenticated"
    assert sched.key_install_count("sta1") == 1


def test_confirm_retransmit_installs_key_once():
    sched = _scheduler()
    session = _commit(sched)
    first = sched.confirm("sta1", session["send_id"], session["recv_id"])
    second = sched.confirm("sta1", session["send_id"], session["recv_id"])
    assert second.get("duplicate") is True
    assert sched.key_install_count("sta1") == 1
    assert first["state"] == second["state"] == "authenticated"


def test_wrong_password_and_timeout_share_error():
    sched = _scheduler()
    token = sched.issue_token("sta1")
    with pytest.raises(SaeError) as bad_password:
        sched.commit("sta1", token, scalar=SCALAR, element=ELEMENT,
                     password="wrong")
    session = _commit(sched)
    sched.advance(31)
    assert sched.expire() == ["sta1"]
    with pytest.raises(SaeError) as timed_out:
        sched.confirm("sta1", session["send_id"], session["recv_id"])
    assert bad_password.value.code == "authentication-failed"
    assert timed_out.value.code == "authentication-failed"


def test_half_open_expires_and_restart_cleans():
    sched = _scheduler()
    _commit(sched)
    snapshot = sched.snapshot()
    restored = _scheduler()
    restored.restore(snapshot)
    assert restored.session_state("sta1") == "committed"
    restored.advance(31)
    assert restored.expire() == ["sta1"]
    assert restored.session_state("sta1") == "none"


def test_pmksa_bound_to_ssid_and_pmkid():
    sched = _scheduler()
    sched.cache_pmksa("sta1", "office", "pmkid-1", b"k" * 32)
    assert sched.lookup_pmksa("sta1", "office", "pmkid-1") is True
    assert sched.lookup_pmksa("sta1", "guest", "pmkid-1") is False
    assert sched.lookup_pmksa("sta1", "office", "pmkid-2") is False


def test_group_switch_clears_old_sae_state():
    sched = _scheduler()
    first = _commit(sched, group=19)
    sched.confirm("sta1", first["send_id"], first["recv_id"])
    second = _commit(sched, group=20)
    assert sched.current_group("sta1") == 20
    assert second["session_id"] != first["session_id"]
    assert sched.session_state("sta1") == "committed"
    with pytest.raises(SaeError):
        sched.confirm("sta1", first["send_id"], first["recv_id"])


def test_concurrent_same_mac_single_state():
    sched = _scheduler()
    token = sched.issue_token("sta1")
    barrier = threading.Barrier(2)
    results = []
    errors = []
    lock = threading.Lock()

    def worker():
        barrier.wait()
        try:
            result = sched.commit("sta1", token, scalar=SCALAR,
                                  element=ELEMENT, password="right")
            with lock:
                results.append(result)
        except SaeError as exc:
            with lock:
                errors.append(exc.code)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(results) + len(errors) == 2
    assert len({item["session_id"] for item in results}) <= 1
    assert sched.session_state("sta1") == "committed"


def test_confirm_requires_committed_state():
    sched = _scheduler()
    with pytest.raises(SaeError) as exc:
        sched.confirm("sta1", "send", "recv")
    assert exc.value.code == "invalid-state"
    assert sched.key_install_count("sta1") == 0
