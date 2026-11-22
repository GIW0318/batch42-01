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
        self._templates = {}
        self._pending = []
        self._sequences = {}
        self._resync_domains = set()

    def add_template(self, domain, template_id, fields, version=1, scope_fields=None):
        fields = list(fields)
        scope_fields = list(scope_fields) if scope_fields else []
        if len(fields) > self.max_fields:
            raise IpfixError(
                TOO_MANY_FIELDS,
                "template has %d fields, max is %d" % (len(fields), self.max_fields),
                template_id=template_id,
            )
        key = (domain, template_id)
        existing = self._templates.get(key)
        if existing is not None:
            if version < existing["version"]:
                raise IpfixError(
                    STALE_TEMPLATE,
                    "version %d older than current %d" % (version, existing["version"]),
                    template_id=template_id,
                )
            if version == existing["version"]:
                same = (
                    existing["fields"] == fields
                    and existing["scope_fields"] == scope_fields
                )
                if not same:
                    raise IpfixError(
                        TEMPLATE_CONFLICT,
                        "conflicting redefinition at version %d" % version,
                        template_id=template_id,
                    )
                return
        self._templates[key] = {
            "domain": domain,
            "template_id": template_id,
            "fields": fields,
            "version": version,
            "scope_fields": scope_fields,
        }

    def withdraw_template(self, domain, template_id, version=None):
        self._templates.pop((domain, template_id), None)

    def has_template(self, domain, template_id):
        return (domain, template_id) in self._templates

    def _get_template(self, domain, template_id):
        template = self._templates.get((domain, template_id))
        if template is None:
            raise IpfixError(
                UNKNOWN_TEMPLATE,
                "no template for domain %s" % domain,
                template_id=template_id,
            )
        return template

    @staticmethod
    def _decode_field(field, payload, offset, template_id):
        if field.get("variable"):
            if offset >= len(payload):
                raise IpfixError(
                    TRUNCATED, "missing variable length prefix", template_id=template_id
                )
            length = payload[offset]
            if length == 255:
                raise IpfixError(
                    BAD_VARLEN,
                    "extended variable length not supported",
                    template_id=template_id,
                )
            start = offset + 1
            end = start + length
            if end > len(payload):
                raise IpfixError(
                    TRUNCATED, "variable field overruns record", template_id=template_id
                )
            return payload[start:end].decode("utf-8", "replace"), end
        length = field["length"]
        end = offset + length
        if end > len(payload):
            raise IpfixError(
                TRUNCATED, "fixed field overruns record", template_id=template_id
            )
        raw = payload[offset:end]
        name = field.get("name", "")
        if length == 4 and name.endswith("IPv4Address"):
            return ".".join(str(b) for b in raw), end
        return int.from_bytes(raw, "big"), end

    def decode_record(self, domain, template_id, payload):
        template = self._get_template(domain, template_id)
        offset = 0
        scope = {}
        for field in template["scope_fields"]:
            value, offset = self._decode_field(field, payload, offset, template_id)
            scope[field["name"]] = value
        fields = {}
        for field in template["fields"]:
            value, offset = self._decode_field(field, payload, offset, template_id)
            fields[field["name"]] = value
        return {"scope": scope, "fields": fields}

    def ingest_record(self, domain, template_id, payload):
        if not self.has_template(domain, template_id):
            self._pending.append((domain, template_id, bytes(payload)))
            self._resync_domains.add(domain)
            return None
        return self.decode_record(domain, template_id, payload)

    def drain_pending(self):
        drained = []
        remaining = []
        for domain, template_id, payload in self._pending:
            if self.has_template(domain, template_id):
                drained.append(
                    {
                        "domain": domain,
                        "template_id": template_id,
                        "record": self.decode_record(domain, template_id, payload),
                    }
                )
            else:
                remaining.append((domain, template_id, payload))
        self._pending = remaining
        return drained

    def pending_count(self):
        return len(self._pending)

    def sequence_status(self, domain, seq):
        last = self._sequences.get(domain)
        if last is None:
            self._sequences[domain] = seq
            return {"status": "ok", "lost": 0}
        self._sequences[domain] = seq
        if last == 0xFFFFFFFF and seq <= 0xFFFFFFFF:
            if seq < last:
                return {"status": "wrap", "lost": seq}
        if seq == last + 1:
            return {"status": "ok", "lost": 0}
        if seq > last + 1:
            return {"status": "gap", "lost": seq - last - 1}
        return {"status": "out-of-order", "lost": 0}

    def parse_message(self, data):
        if len(data) < 16:
            raise IpfixError(
                TRUNCATED, "message header needs 16 bytes, got %d" % len(data)
            )
        version = int.from_bytes(data[0:2], "big")
        length = int.from_bytes(data[2:4], "big")
        if length < 16 or len(data) < length:
            raise IpfixError(
                TRUNCATED,
                "declared length %d exceeds %d available bytes"
                % (length, len(data)),
            )
        return {
            "version": version,
            "length": length,
            "export_time": int.from_bytes(data[4:8], "big"),
            "sequence": int.from_bytes(data[8:12], "big"),
            "domain": int.from_bytes(data[12:16], "big"),
            "sets": data[16:length],
        }

    def resync_requests(self):
        return [{"domain": domain} for domain in sorted(self._resync_domains)]

    def snapshot(self):
        return {
            "templates": [
                {
                    "domain": t["domain"],
                    "template_id": t["template_id"],
                    "fields": [dict(f) for f in t["fields"]],
                    "version": t["version"],
                    "scope_fields": [dict(f) for f in t["scope_fields"]],
                }
                for t in self._templates.values()
            ]
        }

    def restore(self, snapshot):
        for entry in snapshot.get("templates", []):
            key = (entry["domain"], entry["template_id"])
            self._templates[key] = {
                "domain": entry["domain"],
                "template_id": entry["template_id"],
                "fields": [dict(f) for f in entry["fields"]],
                "version": entry["version"],
                "scope_fields": [dict(f) for f in entry.get("scope_fields", [])],
            }
            self._resync_domains.add(entry["domain"])
