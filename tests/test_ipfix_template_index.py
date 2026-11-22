import pytest

import ipfix_template_index
from ipfix_template_index import IpfixError, IpfixTemplateIndex


def _ipfix(**kwargs):
    return IpfixTemplateIndex(**kwargs)


def test_same_template_id_isolated_by_domain():
    index = _ipfix()
    index.add_template(1, 256, [{"name": "sourceIPv4Address", "length": 4}])
    index.add_template(2, 256, [{"name": "sourceTransportPort", "length": 2}])
    first = index.decode_record(1, 256, bytes([10, 0, 0, 1]))
    second = index.decode_record(2, 256, bytes([0, 80]))
    assert first["fields"] == {"sourceIPv4Address": "10.0.0.1"}
    assert second["fields"] == {"sourceTransportPort": 80}


def test_stale_version_rejected():
    index = _ipfix()
    index.add_template(5, 300, [{"name": "a", "length": 1}], version=2)
    with pytest.raises(IpfixError) as exc:
        index.add_template(5, 300, [{"name": "b", "length": 1}], version=1)
    assert exc.value.code == "stale-template"
    assert index.decode_record(5, 300, b"\x05")["fields"] == {"a": 5}


def test_newer_version_replaces_fields():
    index = _ipfix()
    index.add_template(5, 300, [{"name": "a", "length": 1}], version=1)
    index.add_template(5, 300, [{"name": "b", "length": 1}], version=2)
    assert index.decode_record(5, 300, b"\x07")["fields"] == {"b": 7}


def test_same_version_conflicting_order_rejected():
    index = _ipfix()
    index.add_template(5, 300, [{"name": "a", "length": 1}, {"name": "b", "length": 1}])
    with pytest.raises(IpfixError) as exc:
        index.add_template(5, 300, [{"name": "b", "length": 1}, {"name": "a", "length": 1}])
    assert exc.value.code == "template-conflict"


def test_withdraw_invalidates_immediately():
    index = _ipfix()
    index.add_template(5, 300, [{"name": "a", "length": 1}])
    assert index.has_template(5, 300) is True
    index.withdraw_template(5, 300)
    assert index.has_template(5, 300) is False
    with pytest.raises(IpfixError) as exc:
        index.decode_record(5, 300, b"\x01")
    assert exc.value.code == "unknown-template"


def test_data_before_template_buffered_then_drained():
    index = _ipfix()
    assert index.ingest_record(5, 300, b"\x09") is None
    assert index.pending_count() == 1
    index.add_template(5, 300, [{"name": "a", "length": 1}])
    drained = index.drain_pending()
    assert len(drained) == 1
    assert drained[0]["record"]["fields"] == {"a": 9}
    assert index.pending_count() == 0


def test_decode_unknown_template_raises():
    index = _ipfix()
    with pytest.raises(IpfixError) as exc:
        index.decode_record(5, 999, b"\x01")
    assert exc.value.code == "unknown-template"
    assert exc.value.template_id == 999


def test_too_many_fields_raises_with_template_id():
    index = _ipfix(max_fields=4)
    fields = [{"name": "f%d" % n, "length": 1} for n in range(5)]
    with pytest.raises(IpfixError) as exc:
        index.add_template(5, 9, fields)
    assert exc.value.code == "too-many-fields"
    assert exc.value.template_id == 9


def test_variable_length_255_rejected_with_template_id():
    index = _ipfix()
    index.add_template(5, 9, [{"name": "ifName", "variable": True}])
    with pytest.raises(IpfixError) as exc:
        index.decode_record(5, 9, b"\xffabc")
    assert exc.value.code == "bad-varlen"
    assert exc.value.template_id == 9


def test_variable_length_field_decoded():
    index = _ipfix()
    index.add_template(5, 9, [{"name": "ifName", "variable": True}])
    assert index.decode_record(5, 9, b"\x03abc")["fields"] == {"ifName": "abc"}


def test_options_scope_separate_from_fields():
    index = _ipfix()
    index.add_template(
        3,
        300,
        [{"name": "sampledPackets", "length": 4}],
        scope_fields=[{"name": "samplingInterval", "length": 4}],
    )
    decoded = index.decode_record(3, 300, bytes([0, 0, 0, 100, 0, 0, 0, 42]))
    assert decoded["scope"] == {"samplingInterval": 100}
    assert decoded["fields"] == {"sampledPackets": 42}


def test_sequence_gap_detected():
    index = _ipfix()
    assert index.sequence_status(7, 100)["status"] == "ok"
    assert index.sequence_status(7, 101)["status"] == "ok"
    gap = index.sequence_status(7, 104)
    assert gap["status"] == "gap"
    assert gap["lost"] == 2


def test_sequence_wrap_detected():
    index = _ipfix()
    index.sequence_status(8, 0xFFFFFFFF)
    wrapped = index.sequence_status(8, 1)
    assert wrapped["status"] == "wrap"
    assert wrapped["lost"] == 1


def test_truncated_message_rejected():
    index = _ipfix()
    with pytest.raises(IpfixError) as exc:
        index.parse_message(b"\x00\x0a")
    assert exc.value.code == "truncated"
    with pytest.raises(IpfixError) as exc:
        index.parse_message(b"\x00\x0a\x00\x20short")
    assert exc.value.code == "truncated"


def test_restart_restores_templates_and_resync():
    index = _ipfix()
    index.add_template(5, 300, [{"name": "a", "length": 1}])
    snapshot = index.snapshot()
    restored = IpfixTemplateIndex()
    restored.restore(snapshot)
    assert restored.has_template(5, 300) is True
    assert restored.decode_record(5, 300, b"\x03")["fields"] == {"a": 3}
    assert any(item["domain"] == 5 for item in restored.resync_requests())
