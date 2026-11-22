"""polkit action authorization resolver."""

import threading
import time
from collections import deque

UNKNOWN_ACTION = "unknown-action"
IMPLICIT_DENIED = "implicit-denied"
NO_RULE = "no-rule"
RULE_ERROR = "rule-error"
UNKNOWN_SUBJECT = "unknown-subject"
RATE_LIMITED = "rate-limited"
AUTHENTICATION_REQUIRED = "authentication-required"
RULE_DENIED = "rule-denied"


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
        self.rules_version = 1
        self._rules_generation = 0
        self._pid_users = {}
        self._authorization_cache = {}
        self._request_times = {}
        self._audit_entries = []
        self._state_lock = threading.RLock()
        self._audit_lock = threading.Lock()
        self._subject_locks = {}
        self._subject_locks_guard = threading.Lock()

    def group_closure(self, groups):
        pending = list(self._as_values(groups))
        closure = set()
        while pending:
            group = pending.pop()
            if group in closure:
                continue
            closure.add(group)
            parents = self.group_tree.get(group, ())
            pending.extend(self._as_values(parents))
        return closure

    def set_subject_pid(self, pid, user):
        pid_key = self._pid_key(pid)
        lock = self._lock_for_subject(self._subject_lock_key(pid_key, user))
        with lock:
            with self._state_lock:
                self._pid_users[pid_key] = user
                self._authorization_cache.clear()

    def load_rules(self, rules, version=1):
        with self._state_lock:
            self.rules = list(rules or [])
            self.rules_version = version
            self._rules_generation += 1
            self._authorization_cache.clear()

    def resolve(self, subject, action_ids, now=0):
        subject = subject if isinstance(subject, dict) else {}
        actions = self._normalize_actions(action_ids)
        pid_key = self._pid_key(subject.get("pid"))
        lock = self._lock_for_subject(self._subject_lock_key(
            pid_key, subject.get("user")))
        with lock:
            return self._resolve_locked(subject, actions, now)

    def audit_log(self):
        with self._audit_lock:
            return [dict(entry) for entry in self._audit_entries]

    def _resolve_locked(self, subject, action_ids, now):
        with self._state_lock:
            rules = tuple(self.rules)
            actions = dict(self.actions)
            generation = self._rules_generation
            expected_user = self._pid_users.get(self._pid_key(subject.get("pid")))

        if (subject.get("pid") is None or expected_user is None
                or subject.get("user") != expected_user):
            return self._record(subject, action_ids, now, self._deny(
                UNKNOWN_SUBJECT))

        for action_id in action_ids:
            if action_id not in actions:
                return self._record(subject, action_ids, now, self._deny(
                    UNKNOWN_ACTION))

        if not action_ids:
            return self._record(subject, action_ids, now, self._deny(NO_RULE))

        if self._is_rate_limited(subject, now):
            return self._record(subject, action_ids, now, self._deny(
                RATE_LIMITED))

        cache_key = (
            generation,
            self._subject_fingerprint(subject),
            tuple(action_ids),
        )
        with self._state_lock:
            cached = self._authorization_cache.get(cache_key)
        if cached is not None:
            return self._record(subject, action_ids, now, dict(cached))

        closure = self.group_closure(subject.get("groups"))
        outcomes = [
            self._evaluate_action(
                subject, closure, action_id, actions[action_id], rules)
            for action_id in action_ids
        ]
        decision = self._combine(outcomes)

        with self._state_lock:
            if generation == self._rules_generation:
                self._authorization_cache[cache_key] = dict(decision)

        return self._record(subject, action_ids, now, decision)

    def _evaluate_action(self, subject, closure, action_id, config, rules):
        if not isinstance(config, dict):
            return self._deny(UNKNOWN_ACTION)

        if not subject.get("active"):
            return self._deny(IMPLICIT_DENIED)

        implicit = config.get("implicit")
        if implicit in (None, "none", False, "no", "deny"):
            implicit_allowed = None
        elif implicit == "any":
            implicit_allowed = True
        elif implicit == "local":
            implicit_allowed = self._is_local(subject.get("session"))
        elif implicit in (True, "yes", "allow"):
            implicit_allowed = True
        else:
            implicit_allowed = False

        if implicit_allowed is False:
            return self._deny(IMPLICIT_DENIED)

        rule_outcomes = self._evaluate_rules(
            subject, closure, action_id, rules)
        errors = [o for o in rule_outcomes if o["reason"] == RULE_ERROR]
        if errors:
            return errors[0]

        denies = [o for o in rule_outcomes if o["status"] == "no"]
        if denies:
            return denies[0]

        challenges = [o for o in rule_outcomes if o["status"] == "challenge"]
        if challenges:
            return challenges[0]

        allows = [o for o in rule_outcomes if o["status"] == "yes"]
        if config.get("requires_auth"):
            return self._challenge(AUTHENTICATION_REQUIRED)
        if implicit_allowed is True or allows:
            result = self._allow()
            if allows and "rule" in allows[0]:
                result["rule"] = allows[0]["rule"]
            return result

        return self._deny(NO_RULE)

    def _evaluate_rules(self, subject, closure, action_id, rules):
        outcomes = []
        for rule in rules:
            try:
                if not self._rule_targets_action(rule, action_id):
                    continue
                if not self._rule_matches(subject, closure, rule):
                    continue
                if rule.get("script_error"):
                    raise RuntimeError("rule script failed")
                script = rule.get("script")
                if script is not None:
                    if not callable(script):
                        raise RuntimeError("rule script is not callable")
                    if not script({
                            "subject": subject,
                            "action": action_id,
                            "rule": rule,
                    }):
                        continue
                outcome = self._rule_result(rule)
            except Exception:
                rule_id = rule.get("id", "") if isinstance(rule, dict) else ""
                outcomes.append(self._deny(
                    RULE_ERROR, str(rule_id)))
                continue
            outcomes.append(outcome)
        return outcomes

    def _rule_targets_action(self, rule, action_id):
        if not isinstance(rule, dict):
            raise ValueError("rule must be a mapping")
        target = rule.get("action")
        if target is None:
            return False
        if isinstance(target, str):
            return target == action_id
        return action_id in target

    def _rule_matches(self, subject, closure, rule):
        for key in ("user", "users"):
            if key in rule and subject.get("user") not in self._as_values(
                    rule.get(key)):
                return False
        for key in ("group", "groups"):
            if key in rule:
                required = set(self._as_values(rule.get(key)))
                if not required.intersection(closure):
                    return False
        if "session" in rule and subject.get("session") != rule.get("session"):
            return False
        if "pid" in rule and subject.get("pid") != rule.get("pid"):
            return False
        if "active" in rule and bool(subject.get("active")) != bool(
                rule.get("active")):
            return False
        return True

    def _rule_result(self, rule):
        value = str(rule.get("result", "allow")).lower()
        rule_id = rule.get("id")
        if value in ("allow", "yes", "true"):
            return self._allow(rule_id)
        if value in ("deny", "no", "false"):
            return self._deny(RULE_DENIED, rule_id)
        if value in ("challenge", "auth", "authenticate"):
            return self._challenge(AUTHENTICATION_REQUIRED, rule_id)
        raise ValueError("unsupported rule result")

    def _is_rate_limited(self, subject, now):
        if self.max_requests is None:
            return False
        now = time.monotonic() if now is None else now
        key = (subject.get("user"), self._pid_key(subject.get("pid")))
        with self._state_lock:
            timestamps = self._request_times.setdefault(key, deque())
            cutoff = now - self.window_seconds
            while timestamps and timestamps[0] <= cutoff:
                timestamps.popleft()
            if len(timestamps) >= self.max_requests:
                return True
            timestamps.append(now)
        return False

    def _record(self, subject, action_ids, now, decision):
        entry = {
            "user": subject.get("user"),
            "pid": subject.get("pid"),
            "actions": list(action_ids),
            "decision": "allow" if decision["authorized"] else "deny",
            "status": decision["status"],
            "reason": decision["reason"],
            "now": now,
        }
        if "rule" in decision:
            entry["rule"] = decision["rule"]
        with self._audit_lock:
            self._audit_entries.append(entry)
        return decision

    def _subject_fingerprint(self, subject):
        groups = tuple(sorted(
            (self._hashable(value) for value in
             self._as_values(subject.get("groups"))),
            key=repr,
        ))
        return (
            subject.get("user"),
            groups,
            self._hashable(subject.get("session")),
            self._pid_key(subject.get("pid")),
            bool(subject.get("active")),
        )

    def _lock_for_subject(self, key):
        key = self._hashable(key)
        with self._subject_locks_guard:
            return self._subject_locks.setdefault(key, threading.RLock())

    @staticmethod
    def _subject_lock_key(pid_key, user):
        if pid_key is not None:
            return ("pid", pid_key)
        return ("user", user)

    @staticmethod
    def _normalize_actions(action_ids):
        if action_ids is None:
            return []
        if isinstance(action_ids, str):
            return [action_ids]
        try:
            return list(action_ids)
        except TypeError:
            return [action_ids]

    @staticmethod
    def _as_values(value):
        if value is None:
            return ()
        if isinstance(value, str):
            return (value,)
        if isinstance(value, dict):
            return tuple(value.keys())
        try:
            return tuple(value)
        except TypeError:
            return (value,)

    @staticmethod
    def _pid_key(pid):
        if isinstance(pid, bool) or pid is None:
            return pid
        if isinstance(pid, int):
            return pid
        if isinstance(pid, str):
            try:
                return int(pid)
            except ValueError:
                return pid
        return pid

    @staticmethod
    def _is_local(session):
        if isinstance(session, dict):
            session = session.get("type", session.get("kind"))
        return session == "local"

    @classmethod
    def _hashable(cls, value):
        if isinstance(value, dict):
            return tuple(sorted(
                (cls._hashable(k), cls._hashable(v))
                for k, v in value.items()
            ))
        if isinstance(value, (list, tuple, set)):
            return tuple(cls._hashable(item) for item in value)
        return value

    @staticmethod
    def _allow(rule=None):
        result = {"authorized": True, "status": "yes", "reason": "authorized"}
        if rule is not None:
            result["rule"] = rule
        return result

    @staticmethod
    def _deny(reason, rule=None):
        result = {"authorized": False, "status": "no", "reason": reason}
        if rule is not None:
            result["rule"] = rule
        return result

    @staticmethod
    def _challenge(reason, rule=None):
        result = {
            "authorized": False,
            "status": "challenge",
            "reason": reason,
        }
        if rule is not None:
            result["rule"] = rule
        return result

    @staticmethod
    def _combine(outcomes):
        errors = [o for o in outcomes if o["reason"] == RULE_ERROR]
        if errors:
            return errors[0]
        denials = [o for o in outcomes if o["status"] == "no"]
        if denials:
            return denials[0]
        challenges = [o for o in outcomes if o["status"] == "challenge"]
        if challenges:
            return challenges[0]
        allowed = [o for o in outcomes if o["status"] == "yes"]
        if allowed:
            result = {
                "authorized": True,
                "status": "yes",
                "reason": "authorized",
            }
            if "rule" in allowed[0]:
                result["rule"] = allowed[0]["rule"]
            return result
        return PolkitActionResolver._deny(NO_RULE)
