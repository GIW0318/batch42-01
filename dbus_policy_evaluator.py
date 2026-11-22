"""D-Bus policy evaluation with atomic reloads and audited decisions."""

import copy
import threading


ACCESS_DENIED = "org.freedesktop.DBus.Error.AccessDenied"
NOT_SUPPORTED = "org.freedesktop.DBus.Error.NotSupported"
POLICY_NOT_LOADED = "policy-not-loaded"

_MISSING = object()


class DbusError(Exception):
    def __init__(self, code, message=""):
        self.code = code
        super().__init__(code if not message else "%s: %s" % (code, message))


class _PolicyState:
    """An immutable policy snapshot visible to evaluations."""

    __slots__ = ("version", "epoch", "files")

    def __init__(self, version, epoch, files):
        self.version = version
        self.epoch = epoch
        self.files = files


def _freeze(value):
    """Return a hashable representation without losing relevant distinctions."""

    if isinstance(value, dict):
        items = [(_freeze(key), _freeze(item)) for key, item in value.items()]
        items.sort(key=lambda pair: repr(pair[0]))
        return ("dict", tuple(items))
    if isinstance(value, (list, tuple)):
        return ("sequence", tuple(_freeze(item) for item in value))
    if isinstance(value, (set, frozenset)):
        items = [_freeze(item) for item in value]
        items.sort(key=repr)
        return ("set", tuple(items))
    try:
        hash(value)
    except TypeError:
        return ("object", repr(value))
    return value


def _clean_name(value):
    if value is None:
        return ""
    if not isinstance(value, str):
        value = str(value)
    return value.strip()


def _as_bool(value):
    if value is True or value == 1:
        return True
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return False


def _canonical_group(value, default_domain=""):
    """Canonicalize to ``domain:group`` when a domain is available.

    A bare group is qualified with the subject's domain.  This keeps local
    and domain groups with the same short name distinct.
    """

    group = _clean_name(value)
    if not group:
        return ""

    domain = ""
    name = group
    if "\\" in group:
        domain, name = group.split("\\", 1)
    elif ":" in group:
        domain, name = group.split(":", 1)
    elif default_domain:
        domain = _clean_name(default_domain)

    domain = domain.strip()
    name = name.strip()
    if not name:
        return ""
    if domain:
        return "%s:%s" % (domain.casefold(), name)
    return name


class DbusPolicyEvaluator:
    """Policy files evaluated with deny precedence and exact matching.

    A rule matches only when path, interface, member and destination are
    present and equal ("*" is the explicit wildcard).  Multiple policy files
    are intersected.  The name registry maps well-known and unique bus names
    to the same identity for send_destination checks.
    """

    def __init__(self):
        self._lock = threading.RLock()
        self._names = {}
        self._state = None
        self._epoch = 0
        self._cache = {}
        self._audit = []

    def register_name(self, well_known, unique):
        well_known = _clean_name(well_known)
        unique = _clean_name(unique)
        if not well_known or not unique:
            raise ValueError("well-known and unique names must be non-empty")

        with self._lock:
            self._names[well_known] = unique
            self._names[unique] = unique
            self._epoch += 1
            self._cache.clear()

    def load_policy(self, files, version=1):
        """Stage all policy files, then atomically publish one new snapshot."""

        if not isinstance(files, dict):
            raise TypeError("files must be a mapping of name to rules")

        staged_files = []
        for filename, rules in files.items():
            if rules is None:
                rules = []
            if not isinstance(rules, (list, tuple)):
                raise TypeError("policy rules for %s must be a sequence" % filename)

            staged_rules = []
            for rule in rules:
                if not isinstance(rule, dict):
                    raise TypeError("each policy rule must be a mapping")
                staged_rules.append(copy.deepcopy(rule))
            staged_files.append((str(filename), tuple(staged_rules)))

        with self._lock:
            self._epoch += 1
            self._state = _PolicyState(
                version=version,
                epoch=self._epoch,
                files=tuple(staged_files),
            )
            self._cache.clear()
        return version

    def policy_version(self):
        with self._lock:
            if self._state is None:
                return 0
            return self._state.version

    def evaluate(self, message, subject):
        if not isinstance(message, dict):
            raise TypeError("message must be a mapping")
        if not isinstance(subject, dict):
            raise TypeError("subject must be a mapping")

        with self._lock:
            state = self._state
            if state is None:
                result = (False, None, POLICY_NOT_LOADED, (), "policy-not-loaded")
                self._record_audit(message, subject, None, result)
                return self._result_dict(result)

            cache_key = (state.epoch, _freeze(message), _freeze(subject))
            result = self._cache.get(cache_key)
            if result is None:
                result = self._evaluate_state(state, message, subject)
                self._cache[cache_key] = result
                if len(self._cache) > 4096:
                    self._cache.clear()
                    self._cache[cache_key] = result
            self._record_audit(message, subject, state, result)
            return self._result_dict(result)

    def activate_service(self, service):
        with self._lock:
            if self._state is None:
                raise DbusError(POLICY_NOT_LOADED, "policy must be loaded first")
        return True

    def audit_log(self):
        with self._lock:
            return copy.deepcopy(self._audit)

    def _canonical_destination(self, name):
        name = _clean_name(name)
        return self._names.get(name, name)

    def _rule_matches(self, rule, message, subject, eavesdrop):
        action = _clean_name(rule.get("action", "")).lower()
        if action not in ("allow", "deny"):
            return False

        for field in ("path", "interface", "member"):
            expected = rule.get(field, _MISSING)
            actual = message.get(field, _MISSING)
            if expected is _MISSING or actual is _MISSING:
                return False
            expected = _clean_name(expected)
            actual = _clean_name(actual)
            if not expected or not actual:
                return False
            if expected != "*" and expected != actual:
                return False

        expected_destination = rule.get("send_destination", _MISSING)
        actual_destination = message.get("destination", _MISSING)
        if expected_destination is _MISSING or actual_destination is _MISSING:
            return False
        expected_destination = _clean_name(expected_destination)
        actual_destination = _clean_name(actual_destination)
        if not expected_destination or not actual_destination:
            return False
        if expected_destination != "*":
            if self._canonical_destination(expected_destination) != self._canonical_destination(
                actual_destination
            ):
                return False

        if eavesdrop and action == "allow" and not _as_bool(rule.get("eavesdrop", False)):
            return False

        if not self._group_matches(rule, subject):
            return False

        rule_user = rule.get("user", _MISSING)
        if rule_user is not _MISSING:
            users = rule_user if isinstance(rule_user, (list, tuple, set)) else [rule_user]
            users = {_clean_name(user) for user in users if _clean_name(user)}
            if not users or _clean_name(subject.get("user", "")) not in users:
                return False

        return True

    def _group_matches(self, rule, subject):
        group_value = rule.get("group", _MISSING)
        if group_value is _MISSING:
            group_value = rule.get("groups", _MISSING)
        if group_value is _MISSING:
            return True

        if isinstance(group_value, (list, tuple, set)):
            required_groups = list(group_value)
        else:
            required_groups = [group_value]

        subject_domain = _clean_name(subject.get("domain", ""))
        subject_groups = subject.get("groups", [])
        if isinstance(subject_groups, str):
            subject_groups = [subject_groups]
        available = {
            _canonical_group(group, subject_domain)
            for group in subject_groups
            if _canonical_group(group, subject_domain)
        }
        if not available:
            return False

        for group in required_groups:
            canonical = _canonical_group(group, subject_domain)
            if canonical and canonical in available:
                return True
        return False

    @staticmethod
    def _rule_id(rule, filename, index):
        rule_id = rule.get("id")
        if rule_id is not None:
            return rule_id
        return "%s#%d" % (filename, index)

    def _evaluate_state(self, state, message, subject):
        message_type = _clean_name(message.get("type", "")).lower()
        eavesdrop = message_type == "eavesdrop"

        all_files_allow = bool(state.files)
        first_allow = None
        first_deny = None
        matched_ids = []

        for filename, rules in state.files:
            file_has_allow = False
            for index, rule in enumerate(rules):
                if not self._rule_matches(rule, message, subject, eavesdrop):
                    continue

                rule_id = self._rule_id(rule, filename, index)
                action = _clean_name(rule.get("action", "")).lower()
                matched_ids.append(rule_id)
                if action == "deny":
                    if first_deny is None:
                        first_deny = rule_id
                elif action == "allow":
                    file_has_allow = True
                    if first_allow is None:
                        first_allow = rule_id

            if not file_has_allow:
                all_files_allow = False

        if first_deny is not None:
            return (
                False,
                first_deny,
                ACCESS_DENIED,
                tuple(matched_ids),
                "explicit-deny",
            )

        if all_files_allow:
            return (
                True,
                first_allow,
                None,
                tuple(matched_ids),
                "allow",
            )

        if eavesdrop:
            return (
                False,
                None,
                ACCESS_DENIED,
                tuple(matched_ids),
                "eavesdrop-default-deny",
            )

        return (
            False,
            None,
            NOT_SUPPORTED,
            tuple(matched_ids),
            "no-allow-rule",
        )

    @staticmethod
    def _result_dict(result):
        return {
            "allowed": result[0],
            "rule": result[1],
            "error": result[2],
        }

    def _record_audit(self, message, subject, state, result):
        self._audit.append(
            {
                "decision": "allow" if result[0] else "deny",
                "rule": result[1],
                "matched_rules": list(result[3]),
                "error": result[2],
                "reason": result[4],
                "version": None if state is None else state.version,
                "subject": copy.deepcopy(subject),
                "message": copy.deepcopy(message),
            }
        )
