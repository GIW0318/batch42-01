"""SNMP trap/inform deduplication."""

from collections import OrderedDict
from collections.abc import Mapping
import hashlib
import hmac


AUTH_FAILED = "auth-failed"
FORMAT_ERROR = "format-error"
UNKNOWN_ENGINE = "unknown-engine"
UNKNOWN_SEVERITY = "unknown"

_UINT32_MODULUS = 1 << 32
_STAT_KEYS = ("stored", "duplicates", "discarded")
_HEX_DIGITS = frozenset("0123456789abcdefABCDEF")


class SnmpError(Exception):
    def __init__(self, code, message=""):
        self.code = code
        super().__init__(code if not message else "%s: %s" % (code, message))


class SnmpAuthError(SnmpError):
    pass


class SnmpFormatError(SnmpError):
    pass


def _format_error(message):
    return SnmpFormatError(FORMAT_ERROR, message)


def _normalize_engine(engine_id):
    if isinstance(engine_id, memoryview):
        engine_id = engine_id.tobytes()
    if isinstance(engine_id, (bytes, bytearray)):
        raw = bytes(engine_id)
        if not raw:
            raise _format_error("engine must not be empty")
        try:
            text = raw.decode("ascii")
        except UnicodeDecodeError:
            return raw.hex().lower()
        if text and all(char in _HEX_DIGITS or char in ":- " for char in text):
            return _normalize_engine(text)
        return raw.hex().lower()
    if not isinstance(engine_id, str):
        raise _format_error("engine must be a string or bytes")
    value = engine_id.strip()
    if not value:
        raise _format_error("engine must not be empty")
    if value[:2].lower() == "0x":
        value = value[2:]
    compact = value.replace(":", "").replace("-", "").replace(" ", "")
    if not compact:
        raise _format_error("engine must not be empty")
    if all(char in _HEX_DIGITS for char in compact):
        return compact.lower()
    return value.lower()


def _normalize_context(context):
    if context is None:
        return ""
    if isinstance(context, memoryview):
        context = context.tobytes()
    if isinstance(context, (bytes, bytearray)):
        raw = bytes(context)
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError:
            return raw.hex().lower()
    return str(context)


def _normalize_oid(oid):
    if isinstance(oid, memoryview):
        oid = oid.tobytes()
    if isinstance(oid, (bytes, bytearray)):
        try:
            oid = bytes(oid).decode("ascii")
        except UnicodeDecodeError as exc:
            raise _format_error("OID must be ASCII") from exc
    if isinstance(oid, (tuple, list)):
        if not oid or not all(isinstance(part, int) and part >= 0 for part in oid):
            raise _format_error("OID sequence must contain non-negative integers")
        return ".".join(str(part) for part in oid)
    if not isinstance(oid, str):
        raise _format_error("OID must be a string")
    value = oid.strip()
    if value.startswith("."):
        value = value[1:]
    if not value:
        raise _format_error("OID must not be empty")
    parts = value.split(".")
    if any(not part for part in parts):
        raise _format_error("OID contains an empty component")
    if all(part.isdigit() for part in parts):
        return ".".join(str(int(part)) for part in parts)
    return value


def _canonical_value(value):
    if value is None:
        return ("null",)
    if isinstance(value, bool):
        return ("bool", value)
    if isinstance(value, int):
        return ("int", value)
    if isinstance(value, float):
        return ("float", repr(value))
    if isinstance(value, str):
        return ("str", value)
    if isinstance(value, memoryview):
        value = value.tobytes()
    if isinstance(value, (bytes, bytearray)):
        return ("bytes", bytes(value).hex())
    if isinstance(value, (list, tuple)):
        return ("seq", tuple(_canonical_value(item) for item in value))
    if isinstance(value, Mapping):
        items = [
            (_canonical_value(key), _canonical_value(item))
            for key, item in value.items()
        ]
        items.sort(key=repr)
        return ("map", tuple(items))
    raise _format_error("unsupported varbind value type")


def _canonical_varbinds(varbinds):
    if varbinds is None or isinstance(
        varbinds, (str, bytes, bytearray, memoryview, Mapping)
    ):
        raise _format_error("varbinds must be a sequence")
    try:
        iterator = iter(varbinds)
    except TypeError as exc:
        raise _format_error("varbinds must be a sequence") from exc

    normalized = []
    for varbind in iterator:
        if isinstance(varbind, Mapping):
            if "oid" not in varbind or "value" not in varbind:
                raise _format_error("varbind mapping requires oid and value")
            oid = varbind["oid"]
            value = varbind["value"]
        elif isinstance(varbind, (list, tuple)) and len(varbind) == 2:
            oid, value = varbind
        else:
            raise _format_error("varbind must be an oid/value pair")
        normalized.append((_normalize_oid(oid), _canonical_value(value)))
    normalized.sort(key=repr)
    return tuple(normalized)


def _normalize_kind(value):
    if value is None:
        return "trap"
    if isinstance(value, (bytes, bytearray)):
        try:
            value = bytes(value).decode("ascii")
        except UnicodeDecodeError as exc:
            raise _format_error("kind must be ASCII") from exc
    if not isinstance(value, str):
        raise _format_error("kind must be a string")
    return value.strip().lower() or "trap"


def _normalize_request_id(value):
    if value is None:
        return None
    if isinstance(value, bool):
        raise _format_error("request_id must be an integer")
    if isinstance(value, (bytes, bytearray)):
        try:
            value = bytes(value).decode("ascii")
        except UnicodeDecodeError as exc:
            raise _format_error("request_id must be an integer") from exc
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise _format_error("request_id must be an integer") from exc


def _normalize_uptime(value):
    if isinstance(value, bool):
        raise _format_error("uptime must be an integer")
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise _format_error("uptime must be an integer") from exc


def _normalize_timestamp(value, field_name):
    if isinstance(value, bool):
        raise _format_error("%s must be a number" % field_name)
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise _format_error("%s must be a number" % field_name) from exc


def _trap_fields(trap):
    if not isinstance(trap, Mapping):
        raise _format_error("trap must be a mapping")
    if "engine" not in trap or "oid" not in trap or "varbinds" not in trap:
        raise _format_error("trap requires engine, oid and varbinds")

    kind = _normalize_kind(trap.get("kind"))
    request_id = _normalize_request_id(trap.get("request_id"))
    if kind == "inform" and request_id is None:
        raise _format_error("inform requires request_id")

    return {
        "engine": _normalize_engine(trap["engine"]),
        "context": _normalize_context(trap.get("context", "")),
        "oid": _normalize_oid(trap["oid"]),
        "varbinds": _canonical_varbinds(trap["varbinds"]),
        "kind": kind,
        "request_id": request_id,
    }


def _key_from_fields(fields):
    return (
        fields["engine"],
        fields["context"],
        fields["oid"],
        fields["varbinds"],
        fields["kind"],
        fields["request_id"],
    )


def _to_jsonable(value):
    if isinstance(value, Mapping):
        return {str(key): _to_jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_to_jsonable(item) for item in value]
    return value


def _from_jsonable(value):
    if isinstance(value, Mapping):
        return {key: _from_jsonable(item) for key, item in value.items()}
    if isinstance(value, list):
        return tuple(_from_jsonable(item) for item in value)
    return value


class SnmpTrapDeduper:
    """Windowed deduplication keyed by engine, context, OID and varbinds.

    The v3 digest is the full hex HMAC-SHA256 over
    "engine|context|oid|request_id|uptime".  Batches are validated in full
    before any trap is stored.
    """

    def __init__(self, window_seconds=300, max_cache=1000, known_engines=None,
                 severity_map=None):
        try:
            window = float(window_seconds)
            cache_limit = int(max_cache)
        except (TypeError, ValueError) as exc:
            raise ValueError("window_seconds and max_cache must be numeric") from exc
        if window < 0:
            raise ValueError("window_seconds must not be negative")
        if cache_limit < 0:
            raise ValueError("max_cache must not be negative")

        self.window_seconds = window
        self.max_cache = cache_limit
        self._engine_filter_enabled = known_engines is not None
        if known_engines is None:
            engines = ()
        elif isinstance(known_engines, (str, bytes, bytearray, memoryview)):
            engines = (known_engines,)
        else:
            engines = known_engines
        self.known_engines = {_normalize_engine(item) for item in engines}
        self.severity_map = {}
        for oid, level in (severity_map or {}).items():
            self.severity_map[_normalize_oid(oid)] = level
        self._cache = OrderedDict()
        self._stats = {key: 0 for key in _STAT_KEYS}

    def add_engine(self, engine_id):
        normalized = _normalize_engine(engine_id)
        self.known_engines.add(normalized)
        self._engine_filter_enabled = True
        return normalized

    def dedup_key(self, trap):
        return _key_from_fields(_trap_fields(trap))

    def ingest(self, trap, now=0):
        return self.ingest_batch([trap], now=now)[0]

    def ingest_batch(self, traps, now=0):
        if traps is None or isinstance(
            traps, (str, bytes, bytearray, memoryview, Mapping)
        ):
            raise _format_error("traps must be a sequence")
        try:
            batch = list(traps)
        except TypeError as exc:
            raise _format_error("traps must be a sequence") from exc

        now_value = _normalize_timestamp(now, "now")
        old_cache = OrderedDict(self._cache)
        old_stats = dict(self._stats)
        results = []
        try:
            for trap in batch:
                results.append(self._ingest_one(trap, now_value))
        except Exception:
            self._cache = old_cache
            self._stats = old_stats
            raise
        return results

    def _ingest_one(self, trap, now):
        fields = _trap_fields(trap)
        if self._engine_filter_enabled and fields["engine"] not in self.known_engines:
            raise SnmpError(UNKNOWN_ENGINE, "unrecognized engine %s" % fields["engine"])

        received_at = _normalize_timestamp(
            trap.get("received_at", now), "received_at"
        )
        self._purge_expired(now)
        if now - received_at > self.window_seconds:
            self._stats["discarded"] += 1
            return False

        key = _key_from_fields(fields)
        if key in self._cache:
            self._stats["duplicates"] += 1
            self._cache.move_to_end(key)
            self._cache[key] = now
            return False

        self._stats["stored"] += 1
        if self.max_cache == 0:
            return True
        while len(self._cache) >= self.max_cache:
            self._cache.popitem(last=False)
        self._cache[key] = now
        return True

    def _purge_expired(self, now):
        expired = [
            key for key, stored_at in self._cache.items()
            if now - stored_at > self.window_seconds
        ]
        for key in expired:
            del self._cache[key]

    def verify_v3(self, trap, secret):
        fields = _trap_fields(trap)
        if fields["request_id"] is None:
            raise _format_error("SNMPv3 message requires request_id")
        if "uptime" not in trap:
            raise _format_error("SNMPv3 message requires uptime")
        uptime = _normalize_uptime(trap["uptime"])
        canonical = "%s|%s|%s|%s|%s" % (
            fields["engine"],
            fields["context"],
            fields["oid"],
            fields["request_id"],
            uptime,
        )

        if isinstance(secret, str):
            secret_bytes = secret.encode("utf-8")
        elif isinstance(secret, memoryview):
            secret_bytes = secret.tobytes()
        elif isinstance(secret, (bytes, bytearray)):
            secret_bytes = bytes(secret)
        else:
            raise _format_error("secret must be a string or bytes")

        expected_hex = hmac.new(
            secret_bytes, canonical.encode("utf-8"), hashlib.sha256
        ).hexdigest()
        expected_bytes = bytes.fromhex(expected_hex)
        supplied = trap.get("digest")
        if supplied is None:
            raise SnmpAuthError(AUTH_FAILED, "missing SNMPv3 digest")

        if isinstance(supplied, memoryview):
            supplied = supplied.tobytes()
        if isinstance(supplied, (bytes, bytearray)):
            supplied_bytes = bytes(supplied)
            if len(supplied_bytes) == len(expected_bytes):
                valid = hmac.compare_digest(expected_bytes, supplied_bytes)
            else:
                try:
                    supplied = supplied_bytes.decode("ascii")
                except UnicodeDecodeError:
                    raise SnmpAuthError(AUTH_FAILED, "invalid SNMPv3 digest")
                valid = self._compare_hex_digest(expected_hex, supplied)
        elif isinstance(supplied, str):
            valid = self._compare_hex_digest(expected_hex, supplied)
        else:
            valid = False

        if not valid:
            raise SnmpAuthError(AUTH_FAILED, "invalid SNMPv3 digest")
        return True

    @staticmethod
    def _compare_hex_digest(expected_hex, supplied):
        if len(supplied) != len(expected_hex):
            return False
        if any(char not in _HEX_DIGITS for char in supplied):
            return False
        return hmac.compare_digest(expected_hex, supplied.lower())

    def severity(self, oid):
        return self.severity_map.get(_normalize_oid(oid), UNKNOWN_SEVERITY)

    def sort_by_uptime(self, traps):
        if traps is None or isinstance(
            traps, (str, bytes, bytearray, memoryview, Mapping)
        ):
            raise _format_error("traps must be a sequence")
        try:
            batch = list(traps)
        except TypeError as exc:
            raise _format_error("traps must be a sequence") from exc
        if len(batch) < 2:
            return batch

        items = []
        for index, trap in enumerate(batch):
            if not isinstance(trap, Mapping) or "uptime" not in trap:
                raise _format_error("trap requires uptime")
            uptime = _normalize_uptime(trap["uptime"]) % _UINT32_MODULUS
            items.append((uptime, index, trap))
        items.sort(key=lambda item: item[0])

        gaps = [
            (items[(index + 1) % len(items)][0] - item[0]) % _UINT32_MODULUS
            for index, item in enumerate(items)
        ]
        largest_gap = max(gaps)
        if largest_gap == 0:
            return batch
        start = (gaps.index(largest_gap) + 1) % len(items)
        ordered = items[start:] + items[:start]
        return [item[2] for item in ordered]

    def cache_size(self):
        return len(self._cache)

    def stats(self):
        return dict(self._stats)

    def snapshot(self):
        return {
            "version": 1,
            "window_seconds": self.window_seconds,
            "max_cache": self.max_cache,
            "engine_filter_enabled": self._engine_filter_enabled,
            "known_engines": sorted(self.known_engines),
            "severity_map": _to_jsonable(self.severity_map),
            "cache": [
                {"key": _to_jsonable(key), "stored_at": stored_at}
                for key, stored_at in self._cache.items()
            ],
            "stats": dict(self._stats),
        }

    def restore(self, snapshot):
        if not isinstance(snapshot, Mapping):
            raise _format_error("snapshot must be a mapping")

        window = self.window_seconds
        cache_limit = self.max_cache
        if "window_seconds" in snapshot:
            window = _normalize_timestamp(snapshot["window_seconds"], "window_seconds")
            if window < 0:
                raise _format_error("window_seconds must not be negative")
        if "max_cache" in snapshot:
            raw_limit = snapshot["max_cache"]
            if isinstance(raw_limit, bool):
                raise _format_error("max_cache must be an integer")
            try:
                cache_limit = int(raw_limit)
            except (TypeError, ValueError) as exc:
                raise _format_error("max_cache must be an integer") from exc
            if cache_limit < 0:
                raise _format_error("max_cache must not be negative")

        raw_stats = snapshot.get("stats", {})
        if not isinstance(raw_stats, Mapping):
            raise _format_error("snapshot stats must be a mapping")
        new_stats = {}
        for key in _STAT_KEYS:
            value = raw_stats.get(key, 0)
            if isinstance(value, bool):
                raise _format_error("snapshot counters must be integers")
            try:
                value = int(value)
            except (TypeError, ValueError) as exc:
                raise _format_error("snapshot counters must be integers") from exc
            if value < 0:
                raise _format_error("snapshot counters must not be negative")
            new_stats[key] = value

        raw_cache = snapshot.get("cache", [])
        if isinstance(raw_cache, Mapping):
            raw_entries = [
                {"key": key, "stored_at": stored_at}
                for key, stored_at in raw_cache.items()
            ]
        elif isinstance(raw_cache, (list, tuple)):
            raw_entries = raw_cache
        else:
            raise _format_error("snapshot cache must be a sequence")

        new_cache = OrderedDict()
        for entry in raw_entries:
            if not isinstance(entry, Mapping):
                raise _format_error("snapshot cache entry must be a mapping")
            if "key" not in entry or "stored_at" not in entry:
                raise _format_error("snapshot cache entry requires key and stored_at")
            key = _from_jsonable(entry["key"])
            try:
                hash(key)
            except TypeError as exc:
                raise _format_error("snapshot cache key is not hashable") from exc
            stored_at = _normalize_timestamp(entry["stored_at"], "stored_at")
            new_cache[key] = stored_at
            if cache_limit == 0:
                new_cache.clear()
            while len(new_cache) > cache_limit:
                new_cache.popitem(last=False)

        if "known_engines" in snapshot:
            raw_engines = snapshot["known_engines"]
            if isinstance(raw_engines, (str, bytes, bytearray, memoryview)):
                raw_engines = [raw_engines]
            elif not isinstance(raw_engines, (list, tuple, set, frozenset)):
                raise _format_error("snapshot known_engines must be a sequence")
            new_engines = {_normalize_engine(item) for item in raw_engines}
        else:
            new_engines = set(self.known_engines)

        if "severity_map" in snapshot:
            raw_severity = snapshot["severity_map"]
            if not isinstance(raw_severity, Mapping):
                raise _format_error("snapshot severity_map must be a mapping")
            new_severity = {
                _normalize_oid(oid): level for oid, level in raw_severity.items()
            }
        else:
            new_severity = dict(self.severity_map)

        self.window_seconds = window
        self.max_cache = cache_limit
        self._engine_filter_enabled = bool(
            snapshot.get("engine_filter_enabled", self._engine_filter_enabled)
        )
        self.known_engines = new_engines
        self.severity_map = new_severity
        self._cache = new_cache
        self._stats = new_stats
        return self
