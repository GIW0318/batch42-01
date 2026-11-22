"""D-Bus policy evaluation skeleton."""

ACCESS_DENIED = "org.freedesktop.DBus.Error.AccessDenied"
NOT_SUPPORTED = "org.freedesktop.DBus.Error.NotSupported"
POLICY_NOT_LOADED = "policy-not-loaded"


class DbusError(Exception):
    def __init__(self, code, message=""):
        self.code = code
        super().__init__(code if not message else "%s: %s" % (code, message))


class DbusPolicyEvaluator:
    """Policy files evaluated with deny precedence and exact matching.

    A rule matches only when path, interface, member and destination are
    present and equal ("*" is the explicit wildcard).  Multiple policy files
    are intersected.  The name registry maps well-known and unique bus names
    to the same identity for send_destination checks.
    """

    def __init__(self):
        self._names = {}

    def register_name(self, well_known, unique):
        raise NotImplementedError

    def load_policy(self, files, version=1):
        raise NotImplementedError

    def policy_version(self):
        raise NotImplementedError

    def evaluate(self, message, subject):
        raise NotImplementedError

    def activate_service(self, service):
        raise NotImplementedError

    def audit_log(self):
        raise NotImplementedError
