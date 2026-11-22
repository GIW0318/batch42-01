"""SNMP trap/inform deduplication skeleton."""

AUTH_FAILED = "auth-failed"
FORMAT_ERROR = "format-error"
UNKNOWN_ENGINE = "unknown-engine"


class SnmpError(Exception):
    def __init__(self, code, message=""):
        self.code = code
        super().__init__(code if not message else "%s: %s" % (code, message))


class SnmpAuthError(SnmpError):
    pass


class SnmpFormatError(SnmpError):
    pass


class SnmpTrapDeduper:
    """Windowed deduplication keyed by engine, context, OID and varbinds.

    The v3 digest is the full hex HMAC-SHA256 over
    "engine|context|oid|request_id|uptime".  Batches are validated in full
    before any trap is stored.
    """

    def __init__(self, window_seconds=300, max_cache=1000, known_engines=None,
                 severity_map=None):
        self.window_seconds = window_seconds
        self.max_cache = max_cache
        self.known_engines = set(known_engines or ())
        self.severity_map = dict(severity_map or {})

    def add_engine(self, engine_id):
        raise NotImplementedError

    def dedup_key(self, trap):
        raise NotImplementedError

    def ingest(self, trap, now=0):
        raise NotImplementedError

    def ingest_batch(self, traps, now=0):
        raise NotImplementedError

    def verify_v3(self, trap, secret):
        raise NotImplementedError

    def severity(self, oid):
        raise NotImplementedError

    def sort_by_uptime(self, traps):
        raise NotImplementedError

    def cache_size(self):
        raise NotImplementedError

    def stats(self):
        raise NotImplementedError

    def snapshot(self):
        raise NotImplementedError

    def restore(self, snapshot):
        raise NotImplementedError
