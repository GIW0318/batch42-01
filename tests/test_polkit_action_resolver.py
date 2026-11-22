import threading

import pytest

import polkit_action_resolver
from polkit_action_resolver import PolkitActionResolver, PolkitError

ACTIONS = {
    "org.example.read": {"implicit": "any", "requires_auth": False},
    "org.example.restart": {"implicit": "local", "requires_auth": True},
    "org.example.secret": {"implicit": None, "requires_auth": False},
}
GROUPS = {"dev": ["staff"], "staff": ["admins"], "admins": [], "admins-evil": []}


def _resolver(**kwargs):
    options = dict(actions=ACTIONS, group_tree=GROUPS, max_requests=10)
    options.update(kwargs)
    resolver = PolkitActionResolver(**options)
    resolver.set_subject_pid(4242, "alice")
    return resolver


def _subject(**overrides):
    subject = {"user": "alice", "groups": ["dev"], "session": "local",
               "pid": 4242, "active": True}
    subject.update(overrides)
    return subject


def _rule(rule_id, action, result="allow", **fields):
    rule = {"id": rule_id, "action": action, "result": result}
    rule.update(fields)
    return rule


def test_exact_action_not_prefix():
    resolver = _resolver()
    result = resolver.resolve(_subject(), ["org.example.secret-evil"])
    assert result["authorized"] is False
    assert result["reason"] == "unknown-action"


def test_unknown_action_denied():
    resolver = _resolver()
    result = resolver.resolve(_subject(), ["org.example.nope"])
    assert result["status"] == "no"
    assert result["reason"] == "unknown-action"


def test_implicit_local_rejects_remote_session():
    resolver = _resolver()
    result = resolver.resolve(_subject(session="remote"), ["org.example.restart"])
    assert result["authorized"] is False
    assert result["reason"] == "implicit-denied"


def test_nested_group_closure_allows_admin():
    resolver = _resolver(rules=[_rule("r1", "org.example.secret", group="admins")])
    assert "admins" in resolver.group_closure(["dev"])
    result = resolver.resolve(_subject(groups=["dev"]), ["org.example.secret"])
    assert result["authorized"] is True
    assert result["status"] == "yes"


def test_multiple_actions_all_checked():
    resolver = _resolver(rules=[_rule("r1", "org.example.read", group="admins")])
    result = resolver.resolve(_subject(), ["org.example.read", "org.example.secret"])
    assert result["authorized"] is False
    assert result["reason"] == "no-rule"


def test_missing_pid_lookup_denied():
    resolver = _resolver()
    result = resolver.resolve(_subject(pid=9999), ["org.example.read"])
    assert result["authorized"] is False
    assert result["reason"] == "unknown-subject"


def test_challenge_status_not_authorized():
    resolver = _resolver()
    result = resolver.resolve(_subject(), ["org.example.restart"])
    assert result["authorized"] is False
    assert result["status"] == "challenge"


def test_rule_exception_denies():
    resolver = _resolver(rules=[
        _rule("broken", "org.example.secret", script_error=True),
    ])
    result = resolver.resolve(_subject(), ["org.example.secret"])
    assert result["authorized"] is False
    assert result["reason"] == "rule-error"


def test_rule_version_invalidates_cache():
    resolver = _resolver(rules=[_rule("allow", "org.example.secret", group="admins")])
    assert resolver.resolve(_subject(), ["org.example.secret"])["authorized"] is True
    resolver.load_rules([_rule("deny", "org.example.secret", result="deny")], version=2)
    assert resolver.resolve(_subject(), ["org.example.secret"])["authorized"] is False


def test_subject_change_invalidates_cache():
    resolver = _resolver(rules=[
        _rule("allow-admins", "org.example.secret", group="admins"),
        _rule("deny-users", "org.example.secret", result="deny", group="local:users"),
    ])
    denied = resolver.resolve(_subject(groups=["local:users"]), ["org.example.secret"])
    allowed = resolver.resolve(_subject(groups=["dev"]), ["org.example.secret"])
    assert denied["authorized"] is False
    assert allowed["authorized"] is True


def test_rate_limit_enforced_in_backend():
    resolver = _resolver(max_requests=2, window_seconds=60)
    assert resolver.resolve(_subject(), ["org.example.read"], now=0)["authorized"] is True
    assert resolver.resolve(_subject(), ["org.example.read"], now=1)["authorized"] is True
    limited = resolver.resolve(_subject(), ["org.example.read"], now=2)
    assert limited["authorized"] is False
    assert limited["reason"] == "rate-limited"


def test_concurrent_subject_requests_serialized():
    resolver = _resolver(max_requests=1, window_seconds=60)
    barrier = threading.Barrier(2)
    results = []
    lock = threading.Lock()

    def worker():
        barrier.wait()
        try:
            outcome = resolver.resolve(_subject(), ["org.example.read"], now=0)
            with lock:
                results.append(outcome["authorized"])
        except Exception:
            with lock:
                results.append("error")

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(results) == [False, True]


def test_deny_beats_allow():
    resolver = _resolver(rules=[
        _rule("allow", "org.example.secret", group="admins"),
        _rule("deny", "org.example.secret", result="deny", user="alice"),
    ])
    result = resolver.resolve(_subject(), ["org.example.secret"])
    assert result["authorized"] is False
    assert result["rule"] == "deny"


def test_group_prefix_not_matched():
    resolver = _resolver(rules=[
        _rule("allow-admins", "org.example.secret", group="admins"),
    ])
    result = resolver.resolve(_subject(groups=["admins-evil"]), ["org.example.secret"])
    assert result["authorized"] is False


def test_audit_records_reason_and_decision():
    resolver = _resolver()
    resolver.resolve(_subject(), ["org.example.read"])
    resolver.resolve(_subject(session="remote"), ["org.example.restart"])
    entries = resolver.audit_log()
    assert entries[0]["decision"] == "allow"
    assert entries[1]["decision"] == "deny"
    assert entries[1]["reason"] == "implicit-denied"
