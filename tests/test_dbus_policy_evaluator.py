import pytest

import dbus_policy_evaluator
from dbus_policy_evaluator import DbusError, DbusPolicyEvaluator

DEST = "org.example.Service"


def _evaluator():
    evaluator = DbusPolicyEvaluator()
    evaluator.register_name(DEST, ":1.42")
    return evaluator


def _message(**overrides):
    message = {
        "type": "method_call",
        "path": "/org/example/Object",
        "interface": "org.example.Iface",
        "member": "DoThing",
        "destination": DEST,
    }
    message.update(overrides)
    return message


def _subject(**overrides):
    subject = {"user": "alice", "groups": ["local:users"], "domain": "local"}
    subject.update(overrides)
    return subject


def _allow(rule_id="allow-1", **fields):
    rule = {
        "id": rule_id,
        "action": "allow",
        "path": "/org/example/Object",
        "interface": "org.example.Iface",
        "member": "DoThing",
        "send_destination": DEST,
    }
    rule.update(fields)
    return rule


def test_deny_beats_allow_regardless_of_order():
    evaluator = _evaluator()
    evaluator.load_policy({"system.conf": [
        _allow("allow-1"),
        _allow("deny-1", action="deny"),
    ]}, version=1)
    result = evaluator.evaluate(_message(), _subject())
    assert result["allowed"] is False
    assert result["rule"] == "deny-1"


def test_unique_name_normalized_for_destination():
    evaluator = _evaluator()
    evaluator.load_policy({"system.conf": [_allow()]}, version=1)
    assert evaluator.evaluate(_message(destination=":1.42"), _subject())["allowed"] is True


def test_eavesdrop_default_denied():
    evaluator = _evaluator()
    evaluator.load_policy({"system.conf": [_allow()]}, version=1)
    result = evaluator.evaluate(_message(type="eavesdrop"), _subject())
    assert result["allowed"] is False
    assert result["error"] == "org.freedesktop.DBus.Error.AccessDenied"


def test_eavesdrop_explicit_allow():
    evaluator = _evaluator()
    evaluator.load_policy({"system.conf": [
        _allow("eaves-allow", eavesdrop=True),
    ]}, version=1)
    result = evaluator.evaluate(_message(type="eavesdrop"), _subject())
    assert result["allowed"] is True
    assert result["rule"] == "eaves-allow"


def test_exact_path_interface_member_required():
    evaluator = _evaluator()
    evaluator.load_policy({"system.conf": [_allow()]}, version=1)
    assert evaluator.evaluate(_message(member="Other"), _subject())["allowed"] is False
    assert evaluator.evaluate(_message(path="/org/other"), _subject())["allowed"] is False


def test_empty_member_is_not_wildcard():
    evaluator = _evaluator()
    evaluator.load_policy({"system.conf": [_allow(member="")]}, version=1)
    assert evaluator.evaluate(_message(member="DoThing"), _subject())["allowed"] is False
    evaluator.load_policy({"system.conf": [_allow(member="*")]}, version=2)
    assert evaluator.evaluate(_message(member="DoThing"), _subject())["allowed"] is True


def test_groups_qualified_not_name_only():
    evaluator = _evaluator()
    evaluator.load_policy({"system.conf": [
        _allow("allow-group", group="local:admins"),
    ]}, version=1)
    local = evaluator.evaluate(_message(), _subject(groups=["local:admins"]))
    domain = evaluator.evaluate(_message(), _subject(groups=["DOMAIN\\admins"]))
    assert local["allowed"] is True
    assert domain["allowed"] is False


def test_multiple_policy_files_intersect():
    evaluator = _evaluator()
    evaluator.load_policy({
        "system.conf": [_allow("allow-a")],
        "session.conf": [],
    }, version=1)
    assert evaluator.evaluate(_message(), _subject())["allowed"] is False
    evaluator.load_policy({
        "system.conf": [_allow("allow-a")],
        "session.conf": [_allow("allow-b")],
    }, version=2)
    result = evaluator.evaluate(_message(), _subject())
    assert result["allowed"] is True


def test_reload_version_invalidates_cache():
    evaluator = _evaluator()
    evaluator.load_policy({"system.conf": [_allow("allow-1")]}, version=1)
    assert evaluator.evaluate(_message(), _subject())["allowed"] is True
    evaluator.load_policy({"system.conf": [_allow("deny-1", action="deny")]}, version=2)
    assert evaluator.policy_version() == 2
    assert evaluator.evaluate(_message(), _subject())["allowed"] is False


def test_subject_change_invalidates_cache():
    evaluator = _evaluator()
    evaluator.load_policy({"system.conf": [
        _allow("allow-admins", group="local:admins"),
        _allow("deny-users", action="deny", group="local:users"),
    ]}, version=1)
    denied = evaluator.evaluate(_message(), _subject(groups=["local:users"]))
    allowed = evaluator.evaluate(_message(), _subject(groups=["local:admins"]))
    assert denied["allowed"] is False
    assert allowed["allowed"] is True


def test_deny_error_vs_no_rule_error():
    evaluator = _evaluator()
    evaluator.load_policy({"system.conf": [_allow("deny-1", action="deny")]}, version=1)
    denied = evaluator.evaluate(_message(), _subject())
    evaluator.load_policy({"system.conf": []}, version=2)
    no_rule = evaluator.evaluate(_message(), _subject())
    assert denied["error"] == "org.freedesktop.DBus.Error.AccessDenied"
    assert no_rule["error"] == "org.freedesktop.DBus.Error.NotSupported"


def test_audit_records_allow_and_deny():
    evaluator = _evaluator()
    evaluator.load_policy({"system.conf": [
        _allow("allow-1", group="local:users"),
        _allow("deny-1", action="deny", group="local:other"),
    ]}, version=1)
    evaluator.evaluate(_message(), _subject(groups=["local:other"]))
    evaluator.evaluate(_message(), _subject(groups=["local:users"]))
    decisions = [item["decision"] for item in evaluator.audit_log()]
    assert "allow" in decisions
    assert "deny" in decisions


def test_service_activation_requires_policy():
    evaluator = _evaluator()
    with pytest.raises(DbusError) as exc:
        evaluator.activate_service(DEST)
    assert exc.value.code == "policy-not-loaded"
    evaluator.load_policy({"system.conf": [_allow()]}, version=1)
    assert evaluator.activate_service(DEST) is True


def test_unregistered_unique_destination_denied():
    evaluator = _evaluator()
    evaluator.load_policy({"system.conf": [_allow()]}, version=1)
    result = evaluator.evaluate(_message(destination=":1.99"), _subject())
    assert result["allowed"] is False


def test_allow_happy_path():
    evaluator = _evaluator()
    evaluator.load_policy({"system.conf": [_allow()]}, version=1)
    result = evaluator.evaluate(_message(), _subject())
    assert result["allowed"] is True
    assert result["error"] is None
    assert result["rule"] == "allow-1"
