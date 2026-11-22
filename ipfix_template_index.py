"""IPFIX template index skeleton."""

UNKNOWN_TEMPLATE = "unknown-template"
STALE_TEMPLATE = "stale-template"
TEMPLATE_CONFLICT = "template-conflict"
TOO_MANY_FIELDS = "too-many-fields"
BAD_VARLEN = "bad-varlen"
TRUNCATED = "truncated"


class IpfixError(Exception):
    def __init__(self, code, message="", template_id=None):
        self.code = code
        self.template_id = template_id
        super().__init__(code if not message else "%s: %s" % (code, message))


class IpfixTemplateIndex:
    """Templates isolated by (observationDomainId, templateId).

    Template fields are dicts like {"name": "sourceIPv4Address", "length": 4}
    or {"name": "ifName", "variable": True}.  decode_record returns
    {"scope": {...}, "fields": {...}}.
    """

    def __init__(self, max_fields=64):
        self.max_fields = max_fields

    def add_template(self, domain, template_id, fields, version=1, scope_fields=None):
        raise NotImplementedError

    def withdraw_template(self, domain, template_id, version=None):
        raise NotImplementedError

    def has_template(self, domain, template_id):
        raise NotImplementedError

    def decode_record(self, domain, template_id, payload):
        raise NotImplementedError

    def ingest_record(self, domain, template_id, payload):
        raise NotImplementedError

    def drain_pending(self):
        raise NotImplementedError

    def pending_count(self):
        raise NotImplementedError

    def sequence_status(self, domain, seq):
        raise NotImplementedError

    def parse_message(self, data):
        raise NotImplementedError

    def resync_requests(self):
        raise NotImplementedError

    def snapshot(self):
        raise NotImplementedError

    def restore(self, snapshot):
        raise NotImplementedError
