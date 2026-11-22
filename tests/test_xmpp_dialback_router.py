import pytest

import xmpp_dialback_router
from xmpp_dialback_router import XmppDialbackRouter, XmppError

SECRET = b"dialback-secret"


def _router(**kwargs):
    return XmppDialbackRouter(**kwargs)


def _stanza(**overrides):
    stanza = {
        "from": "alice@example.com/phone",
        "to": "bob@example.com/laptop",
        "id": "m1",
        "body": "hello",
        "stream": "s1",
        "type": "chat",
    }
    stanza.update(overrides)
    return stanza


def test_normalize_jid_case_and_escape():
    router = _router()
    assert router.normalize_jid("Alice@Example.COM/Phone") == "alice@example.com/Phone"
    assert router.normalize_jid("alice\\20smith@Example.com") == "alice smith@example.com"


def test_dialback_key_bound_to_domain_pair():
    router = _router()
    key = router.dialback_key("a.example", "b.example", "stream-1", SECRET)
    assert router.verify_dialback("a.example", "b.example", "stream-1", key, SECRET) is True
    assert router.dialback_key("b.example", "a.example", "stream-1", SECRET) != key


def test_verify_dialback_idempotent_and_cached():
    router = _router()
    key = router.dialback_key("a.example", "b.example", "stream-1", SECRET)
    assert router.verify_dialback("a.example", "b.example", "stream-1", key, SECRET)
    assert router.verify_dialback("a.example", "b.example", "stream-1", key, SECRET)
    entries = [item for item in router.audit_log() if item["kind"] == "dialback"]
    assert len(entries) == 1


def test_wrong_dialback_key_rejected():
    router = _router()
    good = router.dialback_key("a.example", "b.example", "stream-1", SECRET)
    other = router.dialback_key("a.example", "b.example", "stream-1", b"other")
    assert good != other
    assert router.verify_dialback("a.example", "b.example", "stream-1",
                                  other, SECRET) is False


def test_unverified_remote_domain_rejected():
    router = _router()
    with pytest.raises(XmppError) as exc:
        router.route(_stanza(to="bob@b.example/laptop"))
    assert exc.value.code == "not-authorized"


def test_route_uses_resource_not_only_domain():
    router = _router()
    router.bind_resource("bob@example.com/laptop", priority=5)
    router.bind_resource("bob@example.com/phone", priority=5)
    result = router.route(_stanza(to="bob@example.com/phone"))
    assert result["deliveries"] == ["bob@example.com/phone"]


def test_equal_priority_delivers_to_all_highest():
    router = _router()
    router.bind_resource("bob@example.com/laptop", priority=5)
    router.bind_resource("bob@example.com/phone", priority=5)
    router.bind_resource("bob@example.com/tablet", priority=1)
    result = router.route(_stanza(to="bob@example.com"))
    assert sorted(result["deliveries"]) == [
        "bob@example.com/laptop",
        "bob@example.com/phone",
    ]


def test_offline_queue_persists_across_restart():
    router = _router()
    assert router.offline_enqueue("bob@example.com", _stanza()) is True
    snapshot = router.snapshot()
    restored = _router()
    restored.restore(snapshot)
    messages = restored.drain_offline("bob@example.com")
    assert len(messages) == 1
    assert messages[0]["id"] == "m1"


def test_offline_queue_full_raises():
    router = _router(max_offline=1)
    assert router.offline_enqueue("bob@example.com", _stanza(id="m1")) is True
    with pytest.raises(XmppError) as exc:
        router.offline_enqueue("bob@example.com", _stanza(id="m2"))
    assert exc.value.code == "queue-full"


def test_sasl_external_requires_xmpp_addr():
    router = _router()
    with pytest.raises(XmppError) as exc:
        router.sasl_external({"subject": "CN=device"})
    assert exc.value.code == "not-authorized"
    assert router.sasl_external(
        {"xmppAddr": ["Alice@Example.com"]}
    ) == "alice@example.com"


def test_sasl_external_other_domain_rejected():
    router = _router()
    with pytest.raises(XmppError) as exc:
        router.sasl_external({"xmppAddr": ["alice@other.example"]})
    assert exc.value.code == "not-authorized"


def test_stanza_size_limit():
    router = _router(max_stanza_bytes=1024)
    router.bind_resource("bob@example.com/laptop", priority=5)
    with pytest.raises(XmppError) as exc:
        router.route(_stanza(body="x" * 2048))
    assert exc.value.code == "stanza-too-large"


def test_hop_limit():
    router = _router(max_hops=10)
    with pytest.raises(XmppError) as exc:
        router.route(_stanza(), hops=11)
    assert exc.value.code == "hop-limit"


def test_duplicate_stanza_once_and_restart_resets():
    router = _router()
    router.bind_resource("bob@example.com/laptop", priority=5)
    first = router.route(_stanza())
    second = router.route(_stanza())
    assert first["deliveries"] == ["bob@example.com/laptop"]
    assert second["duplicate"] is True
    assert second["deliveries"] == []
    router.restart_stream("s1")
    third = router.route(_stanza())
    assert third["deliveries"] == ["bob@example.com/laptop"]


def test_audit_log_uses_normalized_jid_and_decision():
    router = _router()
    router.bind_resource("bob@example.com/laptop", priority=5)
    router.route(_stanza(**{"from": "Alice@Example.COM/Phone"}))
    entry = router.audit_log()[-1]
    assert entry["kind"] == "route"
    assert entry["from"] == "alice@example.com/Phone"
    assert entry["decision"] == "delivered"
