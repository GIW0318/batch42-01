"""Sudoers alias resolution and command matching compiler.

The compiler parses the *entire* policy text first (including alias
cycle validation) and only then installs the resulting immutable policy
for matching.  Nothing is applied while parsing is still in progress.
"""

import fnmatch
import hashlib
import posixpath
import re
import threading


class SudoersError(Exception):
    def __init__(self, code, message=""):
        self.code = code
        super().__init__(code if not message else "%s: %s" % (code, message))


_ALIAS_KINDS = ("User_Alias", "Host_Alias", "Cmnd_Alias", "Runas_Alias")
_TAG_NAMES = ("NOPASSWD", "PASSWD", "NOEXEC", "EXEC", "SETENV", "NOSETENV")
_SHELL_METACHARS = frozenset(";&|$`<>\n\r")
_WILDCARD_CHARS = frozenset("*?[")

_ALIAS_RE = re.compile(
    r"^(User_Alias|Host_Alias|Cmnd_Alias|Runas_Alias)\s+"
    r"([A-Za-z0-9_]+)\s*=\s*(.+)$"
)
_DEFAULTS_RE = re.compile(r"^Defaults(?::([^\s]+))?\s+(.+)$")
_TAG_RE = re.compile(r"^([A-Z]+)\s*:\s*")


def _normalize_path(path):
    if path.startswith("/"):
        return posixpath.normpath(path)
    return path


def _has_wildcard(pattern):
    return any(ch in _WILDCARD_CHARS for ch in pattern)


def _args_are_safe(args):
    for arg in args:
        if any(ch in _SHELL_METACHARS for ch in arg):
            return False
    return True


class _CommandSpec(object):
    __slots__ = ("negated", "pattern", "args", "nopasswd")

    def __init__(self, negated, pattern, args, nopasswd):
        self.negated = negated
        self.pattern = pattern
        self.args = args
        self.nopasswd = nopasswd


class _Rule(object):
    __slots__ = ("users", "hosts", "commands")

    def __init__(self, users, hosts, commands):
        self.users = users
        self.hosts = hosts
        self.commands = commands


class _Policy(object):
    __slots__ = ("aliases", "defaults", "rules", "groups", "digest")

    def __init__(self, aliases, defaults, rules, groups, digest):
        self.aliases = aliases
        self.defaults = defaults
        self.rules = rules
        self.groups = groups
        self.digest = digest


def _split_list(text):
    return [item.strip() for item in text.split(",") if item.strip()]


def _parse_command_spec(item, lineno):
    nopasswd = None
    while True:
        match = _TAG_RE.match(item)
        if match and match.group(1) in _TAG_NAMES:
            tag = match.group(1)
            if tag == "NOPASSWD":
                nopasswd = True
            elif tag == "PASSWD":
                nopasswd = False
            item = item[match.end():]
        else:
            break
    negated = False
    while item.startswith("!"):
        negated = not negated
        item = item[1:].lstrip()
    tokens = item.split()
    if not tokens:
        raise SudoersError(
            "SYNTAX_ERROR", "line %d: empty command entry" % lineno
        )
    pattern = tokens[0]
    args = tokens[1:] if len(tokens) > 1 else None
    if args == [""]:
        args = []
    return _CommandSpec(negated, pattern, args, nopasswd)


def _parse_command_list(rhs, lineno):
    rhs = rhs.strip()
    if rhs.startswith("("):
        depth = 0
        end = -1
        for index, char in enumerate(rhs):
            if char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
                if depth == 0:
                    end = index
                    break
        if end < 0:
            raise SudoersError(
                "SYNTAX_ERROR", "line %d: unbalanced runas list" % lineno
            )
        rhs = rhs[end + 1:]
    specs = [
        _parse_command_spec(part, lineno) for part in _split_list(rhs)
    ]
    if not specs:
        raise SudoersError(
            "SYNTAX_ERROR", "line %d: missing command list" % lineno
        )
    return specs


def _parse_user_spec(line, lineno):
    if "=" not in line:
        raise SudoersError(
            "SYNTAX_ERROR", "line %d: cannot parse %r" % (lineno, line)
        )
    lhs, rhs = line.split("=", 1)
    fields = lhs.split()
    if len(fields) != 2 or not rhs.strip():
        raise SudoersError(
            "SYNTAX_ERROR", "line %d: cannot parse %r" % (lineno, line)
        )
    users = _split_list(fields[0])
    hosts = _split_list(fields[1])
    if not users or not hosts:
        raise SudoersError(
            "SYNTAX_ERROR", "line %d: cannot parse %r" % (lineno, line)
        )
    commands = _parse_command_list(rhs, lineno)
    return _Rule(users, hosts, commands)


def _parse_defaults(line, lineno):
    match = _DEFAULTS_RE.match(line)
    if not match:
        raise SudoersError(
            "SYNTAX_ERROR", "line %d: cannot parse %r" % (lineno, line)
        )
    users = _split_list(match.group(1)) if match.group(1) else None
    options = []
    for item in _split_list(match.group(2)):
        if item.startswith("!"):
            options.append((item[1:], False))
        elif "=" in item:
            name, value = item.split("=", 1)
            options.append((name, value))
        else:
            options.append((item, True))
    if not options:
        raise SudoersError(
            "SYNTAX_ERROR", "line %d: cannot parse %r" % (lineno, line)
        )
    return (users, options)


def _detect_cycles(kind, table):
    color = {}

    def visit(name, stack):
        color[name] = 1
        stack.append(name)
        for member in table[name]:
            if member not in table:
                continue
            state = color.get(member, 0)
            if state == 1:
                cycle = stack[stack.index(member):] + [member]
                raise SudoersError(
                    "ALIAS_CYCLE",
                    "%s cycle: %s" % (kind, " -> ".join(cycle)),
                )
            if state == 0:
                visit(member, stack)
        stack.pop()
        color[name] = 2

    for name in table:
        if color.get(name, 0) == 0:
            visit(name, [])


class SudoersCompiler:
    def __init__(self):
        self._lock = threading.Lock()
        self._groups = {}
        self._cache = {}
        self._policy = _Policy(
            {kind: {} for kind in _ALIAS_KINDS}, [], [], {}, ""
        )
        self._audit = []

    def set_groups(self, mapping):
        with self._lock:
            self._groups = {key: list(value) for key, value in mapping.items()}

    def _groups_fingerprint(self):
        items = sorted(
            (key, tuple(sorted(value))) for key, value in self._groups.items()
        )
        return hashlib.sha256(repr(items).encode("utf-8")).hexdigest()

    def _build_policy(self, text, digest):
        aliases = {kind: {} for kind in _ALIAS_KINDS}
        defaults = []
        rules = []
        for lineno, raw in enumerate(text.splitlines(), 1):
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            alias_match = _ALIAS_RE.match(line)
            if alias_match:
                kind, name, members = alias_match.groups()
                aliases[kind][name] = _split_list(members)
                continue
            if line.startswith("Defaults"):
                defaults.append(_parse_defaults(line, lineno))
                continue
            rules.append(_parse_user_spec(line, lineno))
        for kind in _ALIAS_KINDS:
            _detect_cycles(kind, aliases[kind])
        with self._lock:
            groups = {key: list(value) for key, value in self._groups.items()}
        return _Policy(aliases, defaults, rules, groups, digest)

    def compile(self, text):
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        key = (digest, self._groups_fingerprint())
        with self._lock:
            policy = self._cache.get(key)
        if policy is None:
            policy = self._build_policy(text, digest)
            with self._lock:
                self._cache[key] = policy
                self._policy = policy
        else:
            with self._lock:
                self._policy = policy
        return {"digest": digest, "rules": len(policy.rules)}

    def compile_files(self, files):
        combined = "".join(
            "# included from %s\n%s" % (name, content)
            for name, content in files
        )
        return self.compile(combined)

    def _transitive_groups(self, policy, user):
        seen = set()
        stack = [user]
        while stack:
            entity = stack.pop()
            for group in policy.groups.get(entity, ()):
                if group not in seen:
                    seen.add(group)
                    stack.append(group)
        return seen

    def _user_token_matches(self, token, user, groups, aliases, memo):
        if token == "ALL":
            return True
        if token.startswith("%"):
            return token[1:] in groups
        if token in aliases["User_Alias"]:
            if token in memo:
                return memo[token]
            memo[token] = False
            result = any(
                self._user_token_matches(member, user, groups, aliases, memo)
                for member in aliases["User_Alias"][token]
            )
            memo[token] = result
            return result
        return token == user

    def _host_token_matches(self, token, host, aliases, memo):
        if token == "ALL":
            return True
        if token in aliases["Host_Alias"]:
            if token in memo:
                return memo[token]
            memo[token] = False
            result = any(
                self._host_token_matches(member, host, aliases, memo)
                for member in aliases["Host_Alias"][token]
            )
            memo[token] = result
            return result
        return token == host

    def _command_matches(self, spec, command, args, aliases, memo):
        pattern = spec.pattern
        if pattern == "ALL":
            return True
        if pattern in aliases["Cmnd_Alias"]:
            if pattern in memo:
                return memo[pattern]
            memo[pattern] = False
            result = any(
                self._command_matches(
                    _CommandSpec(False, member, None, None),
                    command,
                    args,
                    aliases,
                    memo,
                )
                for member in aliases["Cmnd_Alias"][pattern]
            )
            memo[pattern] = result
            return result
        normalized = _normalize_path(pattern)
        if _has_wildcard(normalized):
            if not _args_are_safe(args):
                return False
            if not fnmatch.fnmatchcase(command, normalized):
                return False
        elif command != normalized:
            return False
        if spec.args is not None:
            if len(spec.args) != len(args):
                return False
            for expected, actual in zip(spec.args, args):
                if not fnmatch.fnmatchcase(actual, expected):
                    return False
        return True

    def check(self, user, host, command, args=()):
        args = list(args)
        command = _normalize_path(command)
        with self._lock:
            policy = self._policy
        aliases = policy.aliases
        groups = self._transitive_groups(policy, user)
        allowed = False
        nopasswd = False
        user_memo = {}
        host_memo = {}
        for rule in policy.rules:
            if not any(
                self._user_token_matches(token, user, groups, aliases, user_memo)
                for token in rule.users
            ):
                continue
            if not any(
                self._host_token_matches(token, host, aliases, host_memo)
                for token in rule.hosts
            ):
                continue
            command_memo = {}
            for spec in rule.commands:
                if self._command_matches(spec, command, args, aliases, command_memo):
                    allowed = not spec.negated
                    nopasswd = bool(spec.nopasswd)
        event = {
            "user": user,
            "host": host,
            "command": command,
            "args": args,
            "allowed": allowed,
            "nopasswd": nopasswd,
        }
        with self._lock:
            self._audit.append(event)
        return {"allowed": allowed, "nopasswd": nopasswd}

    def effective_defaults(self, user):
        with self._lock:
            policy = self._policy
        result = {}
        for targets, options in policy.defaults:
            if targets is not None and user in targets:
                result.update(options)
        for targets, options in policy.defaults:
            if targets is None:
                result.update(options)
        return result

    def audit_events(self):
        with self._lock:
            return list(self._audit)
