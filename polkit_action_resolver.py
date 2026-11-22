"""polkit action authorization resolver skeleton."""

UNKNOWN_ACTION = "unknown-action"
IMPLICIT_DENIED = "implicit-denied"
NO_RULE = "no-rule"
RULE_ERROR = "rule-error"
UNKNOWN_SUBJECT = "unknown-subject"
RATE_LIMITED = "rate-limited"


class PolkitError(Exception):
    def __init__(self, code, message=""):
        self.code = code
        super().__init__(code if not message else "%s: %s" % (code, message))


class PolkitActionResolver:
    """Exact-action authorization with transitive group expansion.

    resolve() returns {"authorized": bool, "status": "yes"|"no"|"challenge",
    "reason": str}.  Lower-case action ids match exactly; no prefix matching.
    Group membership is expanded through group_tree.  Rule errors deny.
    """

    def __init__(self, actions=None, rules=None, group_tree=None,
                 max_requests=5, window_seconds=60):
        self.actions = dict(actions or {})
        self.rules = list(rules or [])
        self.group_tree = dict(group_tree or {})
        self.max_requests = max_requests
        self.window_seconds = window_seconds

    def group_closure(self, groups):
        raise NotImplementedError

    def set_subject_pid(self, pid, user):
        raise NotImplementedError

    def load_rules(self, rules, version=1):
        raise NotImplementedError

    def resolve(self, subject, action_ids, now=0):
        raise NotImplementedError

    def audit_log(self):
        raise NotImplementedError
