import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from pam_stack_eval import PamError, PamStackEvaluator
import pam_stack_eval


def test_include_cycle_is_reported_as_system_error():
    evaluator = PamStackEvaluator()
    evaluator.load_config("a", "auth include b\n")
    evaluator.load_config("b", "auth include a\n")
    with pytest.raises(PamError) as exc:
        evaluator.evaluate("a", "alice", handlers={})
    assert exc.value.code == "PAM_SYSTEM_ERR"


def test_sufficient_success_short_circuits_required_failure():
    evaluator = PamStackEvaluator()
    evaluator.load_config(
        "login",
        "auth sufficient pam_ok.so\n"
        "auth required pam_fail.so\n",
    )
    result = evaluator.evaluate(
        "login", "alice", handlers={"pam_ok.so": True, "pam_fail.so": False}
    )
    assert result["code"] == "PAM_SUCCESS"
    assert "pam_ok.so" in result["trace"]
    assert "pam_fail.so" not in result["trace"]


def test_later_sufficient_cannot_override_required_failure():
    evaluator = PamStackEvaluator()
    evaluator.load_config(
        "login",
        "auth required pam_fail.so\n"
        "auth sufficient pam_ok.so\n",
    )
    result = evaluator.evaluate(
        "login", "alice", handlers={"pam_fail.so": False, "pam_ok.so": True}
    )
    assert result["code"] == "PAM_AUTH_ERR"


def test_environment_is_isolated_between_concurrent_logins():
    evaluator = PamStackEvaluator()
    evaluator.load_config("login", "auth required pam_env.so\n")
    barrier = threading.Barrier(2)

    def worker(user):
        barrier.wait()
        result = evaluator.evaluate(
            "login",
            user,
            handlers={"pam_env.so": {"auth": True, "env": {"USER": user}}},
        )
        return result["env"]["USER"]

    with ThreadPoolExecutor(max_workers=2) as pool:
        values = list(pool.map(worker, ["alice", "bob"]))
    assert sorted(values) == ["alice", "bob"]


def test_substack_failure_stops_parent_stack():
    evaluator = PamStackEvaluator()
    evaluator.load_config("sub", "auth required pam_sub_fail.so\n")
    evaluator.load_config(
        "main",
        "auth substack sub\n"
        "auth required pam_main_ok.so\n",
    )
    result = evaluator.evaluate(
        "main",
        "alice",
        handlers={"pam_sub_fail.so": False, "pam_main_ok.so": True},
    )
    assert result["code"] == "PAM_AUTH_ERR"
    assert "pam_main_ok.so" not in result["trace"]


def test_success_done_jumps_over_next_module():
    evaluator = PamStackEvaluator()
    evaluator.load_config(
        "login",
        "auth [success=done default=ignore] pam_ok.so\n"
        "auth required pam_fail.so\n",
    )
    result = evaluator.evaluate(
        "login", "alice", handlers={"pam_ok.so": True, "pam_fail.so": False}
    )
    assert result["code"] == "PAM_SUCCESS"
    assert "pam_fail.so" not in result["trace"]


def test_out_of_bounds_jump_is_a_parse_error():
    evaluator = PamStackEvaluator()
    with pytest.raises(PamError) as exc:
        evaluator.load_config(
            "login",
            "auth [success=5 default=ignore] pam_ok.so\n"
            "auth required pam_after.so\n",
        )
    assert exc.value.code == "PAM_SYSTEM_ERR"


def test_quoted_module_arguments_are_parsed_as_one_argument():
    evaluator = PamStackEvaluator()
    config = evaluator.load_config(
        "login", 'auth required pam_foo.so arg="a b" c\n'
    )
    assert config["modules"][0]["args"] == ["a b", "c"]


def test_account_check_runs_before_password_check():
    evaluator = PamStackEvaluator()
    evaluator.load_config(
        "login",
        "password required pam_pw.so\n"
        "account required pam_expire.so\n",
    )
    result = evaluator.evaluate(
        "login",
        "alice",
        handlers={
            "pam_pw.so": True,
            "pam_expire.so": "PAM_ACCOUNT_EXPIRED",
        },
    )
    assert result["code"] == "PAM_ACCOUNT_EXPIRED"
    assert result["trace"].index("pam_expire.so") < result["trace"].index("pam_pw.so")


def test_session_failure_closes_opened_modules_in_reverse():
    evaluator = PamStackEvaluator()
    evaluator.load_config(
        "login",
        "session required pam_sa.so\n"
        "session required pam_sb.so\n"
        "session required pam_sc.so\n",
    )
    result = evaluator.evaluate(
        "login",
        "alice",
        handlers={"pam_sa.so": True, "pam_sb.so": True, "pam_sc.so": False},
    )
    assert result["code"] == "PAM_SESSION_ERR"
    assert result["closed"] == ["pam_sb.so", "pam_sa.so"]


def test_multiple_config_files_are_applied_in_explicit_order():
    evaluator = PamStackEvaluator()
    evaluator.load_configs(
        "login",
        [
            ("10-base", "auth required pam_a.so\n"),
            ("20-more", "auth required pam_b.so\n"),
        ],
    )
    result = evaluator.evaluate(
        "login", "alice", handlers={"pam_a.so": True, "pam_b.so": True}
    )
    assert result["trace"] == ["pam_a.so", "pam_b.so"]


def test_audit_never_records_password():
    evaluator = PamStackEvaluator()
    evaluator.load_config("login", "auth required pam_a.so\n")
    evaluator.evaluate(
        "login", "alice", password="hunter2", handlers={"pam_a.so": True}
    )
    events = evaluator.audit_events()
    assert events
    assert "hunter2" not in str(events)


def test_empty_stack_is_rejected():
    evaluator = PamStackEvaluator()
    evaluator.load_config("login", "")
    result = evaluator.evaluate("login", "alice", handlers={})
    assert result["code"] == "PAM_SYSTEM_ERR"


def test_invalid_config_line_is_a_parse_error():
    evaluator = PamStackEvaluator()
    with pytest.raises(PamError) as exc:
        evaluator.load_config("login", "auth\n")
    assert exc.value.code == "PAM_SYSTEM_ERR"


def test_hot_reload_switches_to_new_stack_version():
    evaluator = PamStackEvaluator()
    first = evaluator.load_config("login", "auth required pam_a.so\n")
    before = evaluator.evaluate(
        "login", "alice", handlers={"pam_a.so": True}
    )
    second = evaluator.load_config("login", "auth required pam_b.so\n")
    after = evaluator.evaluate(
        "login", "alice", handlers={"pam_b.so": True}
    )
    assert second["version"] > first["version"]
    assert before["trace"] == ["pam_a.so"]
    assert after["trace"] == ["pam_b.so"]
