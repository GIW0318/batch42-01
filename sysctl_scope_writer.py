"""Namespace-aware sysctl writer skeleton."""

NOT_NAMESPACED = "not-namespaced"
READ_ONLY = "read-only"
DEPRECATED_PARAMETER = "deprecated-parameter"
INVALID_VALUE = "invalid-value"
OUT_OF_RANGE = "out-of-range"
DEPENDENCY_ORDER = "dependency-order"
INVALID_SCOPE = "invalid-scope"


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

    def set(self, namespace, name, value):
        raise NotImplementedError

    def get(self, namespace, name):
        raise NotImplementedError

    def set_many(self, namespace, assignments):
        raise NotImplementedError

    def audit_log(self):
        raise NotImplementedError

    def snapshot(self):
        raise NotImplementedError

    def restore(self, snapshot):
        raise NotImplementedError
