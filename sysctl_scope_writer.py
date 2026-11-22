"""Namespace-aware sysctl writer.

Writes are routed by the parameter's declared scope: namespaced parameters
have independent values per namespace while non-namespaced parameters can
only be touched on the host.  Batch writes validate every assignment up
front and roll back completely on the first failure.
"""

import re
import threading

NOT_NAMESPACED = "not-namespaced"
READ_ONLY = "read-only"
DEPRECATED_PARAMETER = "deprecated-parameter"
INVALID_VALUE = "invalid-value"
OUT_OF_RANGE = "out-of-range"
DEPENDENCY_ORDER = "dependency-order"
INVALID_SCOPE = "invalid-scope"

_SCOPE_RE = re.compile(r"^(?:host|container:\S+)$")
_VALUE_RE = re.compile(r"^([+-]?\d+)\s*([kKmMgGtT]?[bB]?)$")
_UNIT_MULTIPLIERS = {
    "": 1,
    "k": 1024,
    "m": 1024 ** 2,
    "g": 1024 ** 3,
    "t": 1024 ** 4,
}


class SysctlError(Exception):
    def __init__(self, code, message=""):
        self.code = code
        super().__init__(code if not message else "%s: %s" % (code, message))


class SysctlScopeWriter:
    """Writes sysctl values into the namespace where they belong.

    "host" is the global namespace; other namespaces are "container:<id>".
    Non-namespaced parameters are rejected outside host.  set_many() is
    all-or-nothing, values are parsed by declared type and range, and
    dependencies must already be satisfied in the target namespace.
    """

    def __init__(self, params=None, namespace="host"):
        self.params = dict(params or {})
        self.namespace = namespace
        self._values = {}
        self._audit = []
        self._locks = {}
        self._locks_guard = threading.Lock()

    def _validate_scope(self, namespace):
        if not isinstance(namespace, str) or not _SCOPE_RE.match(namespace):
            raise SysctlError(
                INVALID_SCOPE, "unknown namespace %r" % (namespace,))

    def _lock_for(self, namespace):
        with self._locks_guard:
            lock = self._locks.get(namespace)
            if lock is None:
                lock = threading.Lock()
                self._locks[namespace] = lock
            return lock

    def _parse_value(self, spec, raw):
        if not isinstance(raw, str):
            raw = str(raw)
        match = _VALUE_RE.match(raw.strip())
        if match is None:
            raise SysctlError(
                INVALID_VALUE,
                "cannot parse %r for %s" % (raw, spec.get("name", "?")),
            )
        number = int(match.group(1))
        suffix = match.group(2).lower().rstrip("b")
        number *= _UNIT_MULTIPLIERS[suffix]
        minimum = spec["min"]
        maximum = spec["max"]
        if number < minimum or number > maximum:
            raise SysctlError(
                OUT_OF_RANGE,
                "%s is outside [%s, %s]" % (number, minimum, maximum),
            )
        return str(number)

    def _check_assignment(self, namespace, name, raw):
        spec = self.params.get(name)
        if spec is None:
            raise SysctlError(INVALID_VALUE, "unknown parameter %s" % name)
        if namespace != "host" and not spec.get("namespaced", False):
            raise SysctlError(
                NOT_NAMESPACED,
                "%s is not namespaced and cannot be set from %s"
                % (name, namespace),
            )
        if not spec.get("writable", True):
            raise SysctlError(READ_ONLY, "%s is read-only" % name)
        if spec.get("deprecated", False):
            raise SysctlError(
                DEPRECATED_PARAMETER, "%s is deprecated" % name)
        spec = dict(spec, name=name)
        return self._parse_value(spec, raw)

    def set(self, namespace, name, value):
        self._validate_scope(namespace)
        lock = self._lock_for(namespace)
        with lock:
            canonical = self._check_assignment(namespace, name, value)
            self._values.setdefault(namespace, {})[name] = canonical
            self._audit.append({
                "namespace": namespace,
                "name": name,
                "value": canonical,
            })
            return canonical

    def get(self, namespace, name):
        self._validate_scope(namespace)
        stored = self._values.get(namespace)
        if stored is not None and name in stored:
            return stored[name]
        spec = self.params.get(name)
        if spec is None or namespace != "host":
            return None
        return str(spec["default"])

    def set_many(self, namespace, assignments):
        self._validate_scope(namespace)
        lock = self._lock_for(namespace)
        with lock:
            store = self._values.setdefault(namespace, {})
            previous = dict(store)
            applied = []
            seen = set()
            try:
                for name, raw in assignments:
                    canonical = self._check_assignment(
                        namespace, name, raw)
                    spec = self.params[name]
                    for dependency in spec.get("depends_on", ()):
                        if dependency in seen or dependency in store:
                            continue
                        raise SysctlError(
                            DEPENDENCY_ORDER,
                            "%s requires %s to be configured first"
                            % (name, dependency),
                        )
                    store[name] = canonical
                    applied.append((name, canonical))
                    seen.add(name)
            except Exception:
                if previous:
                    self._values[namespace] = previous
                else:
                    self._values.pop(namespace, None)
                raise
            for name, canonical in applied:
                self._audit.append({
                    "namespace": namespace,
                    "name": name,
                    "value": canonical,
                })
            return {"applied": len(applied)}

    def audit_log(self):
        return [dict(entry) for entry in self._audit]

    def snapshot(self):
        return {
            "values": {
                namespace: dict(store)
                for namespace, store in self._values.items()
            },
        }

    def restore(self, snapshot):
        if not isinstance(snapshot, dict):
            raise SysctlError(INVALID_VALUE, "snapshot must be a mapping")
        values = snapshot.get("values", {})
        self._values = {
            namespace: dict(store) for namespace, store in values.items()
        }
