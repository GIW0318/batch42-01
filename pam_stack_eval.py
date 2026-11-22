"""Strict PAM stack parsing and evaluation."""

import copy
import inspect
import shlex
import threading


PAM_SUCCESS = "PAM_SUCCESS"
PAM_SYSTEM_ERR = "PAM_SYSTEM_ERR"
PAM_AUTH_ERR = "PAM_AUTH_ERR"
PAM_ACCOUNT_ERR = "PAM_ACCOUNT_ERR"
PAM_AUTHTOK_ERR = "PAM_AUTHTOK_ERR"
PAM_SESSION_ERR = "PAM_SESSION_ERR"

PHASES = ("auth", "account", "password", "session")
PHASE_FAILURE = {
    "auth": PAM_AUTH_ERR,
    "account": PAM_ACCOUNT_ERR,
    "password": PAM_AUTHTOK_ERR,
    "session": PAM_SESSION_ERR,
}

_SIMPLE_CONTROLS = {
    "required",
    "requisite",
    "sufficient",
    "optional",
    "include",
    "substack",
}

_ACTIONS = {"ignore", "ok", "done", "bad", "die", "reset"}

_CONTROL_KEYS = {
    "success",
    "open_err",
    "symbol_err",
    "service_err",
    "system_err",
    "buf_err",
    "perm_denied",
    "auth_err",
    "cred_insufficient",
    "authinfo_unavail",
    "user_unknown",
    "maxtries",
    "new_authtok_reqd",
    "acct_expired",
    "account_expired",
    "session_err",
    "cred_unavail",
    "cred_expired",
    "cred_err",
    "no_module_data",
    "conv_err",
    "authtok_err",
    "authtok_recover_err",
    "authtok_lock_busy",
    "authtok_disable_aging",
    "try_again",
    "ignore",
    "abort",
    "authtok_expired",
    "module_unknown",
    "bad_item",
    "conv_again",
    "incomplete",
    "default",
}

_CODE_ALIASES = {
    "pam_success": "success",
    "pam_open_err": "open_err",
    "pam_symbol_err": "symbol_err",
    "pam_service_err": "service_err",
    "pam_system_err": "system_err",
    "pam_buf_err": "buf_err",
    "pam_perm_denied": "perm_denied",
    "pam_auth_err": "auth_err",
    "pam_cred_insufficient": "cred_insufficient",
    "pam_authinfo_unavail": "authinfo_unavail",
    "pam_user_unknown": "user_unknown",
    "pam_maxtries": "maxtries",
    "pam_new_authtok_reqd": "new_authtok_reqd",
    "pam_acct_expired": "acct_expired",
    "pam_account_expired": "acct_expired",
    "pam_session_err": "session_err",
    "pam_cred_unavail": "cred_unavail",
    "pam_cred_expired": "cred_expired",
    "pam_cred_err": "cred_err",
    "pam_no_module_data": "no_module_data",
    "pam_conv_err": "conv_err",
    "pam_authtok_err": "authtok_err",
    "pam_authtok_recover_err": "authtok_recover_err",
    "pam_authtok_lock_busy": "authtok_lock_busy",
    "pam_authtok_disable_aging": "authtok_disable_aging",
    "pam_try_again": "try_again",
    "pam_ignore": "ignore",
    "pam_abort": "abort",
    "pam_authtok_expired": "authtok_expired",
    "pam_module_unknown": "module_unknown",
    "pam_bad_item": "bad_item",
    "pam_conv_again": "conv_again",
    "pam_incomplete": "incomplete",
}

_PAM_NUMBERS = {
    0: PAM_SUCCESS,
    1: "PAM_OPEN_ERR",
    2: "PAM_SYMBOL_ERR",
    3: "PAM_SERVICE_ERR",
    4: PAM_SYSTEM_ERR,
    5: "PAM_BUF_ERR",
    6: "PAM_PERM_DENIED",
    7: PAM_AUTH_ERR,
    8: "PAM_CRED_INSUFFICIENT",
    9: "PAM_AUTHINFO_UNAVAIL",
    10: "PAM_USER_UNKNOWN",
    11: "PAM_MAXTRIES",
    12: "PAM_NEW_AUTHTOK_REQD",
    13: "PAM_ACCOUNT_EXPIRED",
    14: PAM_SESSION_ERR,
    15: "PAM_CRED_UNAVAIL",
    16: "PAM_CRED_EXPIRED",
    17: "PAM_CRED_ERR",
    18: "PAM_NO_MODULE_DATA",
    19: "PAM_CONV_ERR",
    20: PAM_AUTHTOK_ERR,
    21: "PAM_AUTHTOK_RECOVER_ERR",
    22: "PAM_AUTHTOK_LOCK_BUSY",
    23: "PAM_AUTHTOK_DISABLE_AGING",
    24: "PAM_TRY_AGAIN",
    25: "PAM_IGNORE",
    26: "PAM_ABORT",
    27: "PAM_AUTHTOK_EXPIRED",
    28: "PAM_MODULE_UNKNOWN",
    29: "PAM_BAD_ITEM",
    30: "PAM_CONV_AGAIN",
    31: "PAM_INCOMPLETE",
}


class PamError(Exception):
    def __init__(self, code, message=""):
        self.code = code
        super().__init__(code if not message else "%s: %s" % (code, message))


def _error(message):
    raise PamError(PAM_SYSTEM_ERR, message)


def _parse_actions(parts):
    actions = {}
    for part in parts:
        if not part:
            continue
        if "=" not in part:
            _error("invalid control expression: %s" % part)
        key, value = part.split("=", 1)
        key = key.strip().lower()
        value = value.strip().lower()
        if not key or key not in _CONTROL_KEYS:
            _error("unknown control key: %s" % key)
        if not value:
            _error("missing control action for %s" % key)
        if value in _ACTIONS:
            actions[key] = value
        else:
            try:
                actions[key] = int(value, 10)
            except ValueError:
                _error("unknown control action: %s" % value)
    if not actions:
        _error("empty control expression")
    return actions


def _extract_control(tokens):
    token = tokens[1]
    if not token.startswith("["):
        control = token.lower()
        if control not in _SIMPLE_CONTROLS:
            _error("unknown control flag: %s" % token)
        return {"kind": control, "actions": None}, tokens[2:]

    parts = []
    index = 1
    found_end = False
    while index < len(tokens):
        current = tokens[index]
        if index == 1:
            current = current[1:]
        if "]" in current:
            before, _, after = current.partition("]")
            if before:
                parts.append(before)
            if after:
                _error("unexpected token after control expression: %s" % after)
            found_end = True
            index += 1
            break
        parts.append(current)
        index += 1
    if not found_end:
        _error("unterminated control expression")

    return {"kind": "actions", "actions": _parse_actions(parts)}, tokens[index:]


def _parse_text(text):
    modules = []
    if text is None:
        return modules
    if not isinstance(text, str):
        _error("configuration must be text")

    for line_number, line in enumerate(text.splitlines(), 1):
        try:
            tokens = shlex.split(line, comments=True, posix=True)
        except ValueError as exc:
            _error("line %d: %s" % (line_number, exc))
        if not tokens:
            continue
        if len(tokens) < 2:
            _error("line %d: incomplete module entry" % line_number)

        module_type = tokens[0].lower()
        if module_type not in PHASES:
            _error("line %d: unknown module type: %s" % (line_number, tokens[0]))

        control, remaining = _extract_control(tokens)
        if not remaining:
            _error("line %d: missing module" % line_number)
        module_name = remaining[0]
        if not module_name:
            _error("line %d: missing module" % line_number)
        args = []
        for arg in remaining[1:]:
            if "=" in arg:
                args.append(arg.split("=", 1)[1])
            else:
                args.append(arg)

        modules.append(
            {
                "type": module_type,
                "control": control,
                "module": module_name,
                "args": args,
                "line": line_number,
            }
        )
    return modules


def _validate_jumps(modules):
    for phase in PHASES:
        stack = [module for module in modules if module["type"] == phase]
        for index, module in enumerate(stack):
            actions = module["control"]["actions"]
            if not actions:
                continue
            for action in actions.values():
                if not isinstance(action, int):
                    continue
                target = index + 1 + action
                if target < 0 or target > len(stack):
                    _error(
                        "control jump out of bounds at line %d"
                        % module.get("line", 0)
                    )


def _public_config(snapshot):
    result = copy.deepcopy(
        {
            "service": snapshot["service"],
            "version": snapshot["version"],
            "modules": [dict(module) for module in snapshot["modules"]],
        }
    )
    result["files"] = list(snapshot.get("files", ()))
    return result


def _is_success_code(code):
    return code == PAM_SUCCESS


def _action_for(actions, code):
    if code == PAM_SUCCESS:
        return actions.get("success", actions.get("default", "ignore"))
    key = _CODE_ALIASES.get(code.lower())
    if key is not None and key in actions:
        return actions[key]
    if key == "acct_expired" and "account_expired" in actions:
        return actions["account_expired"]
    return actions.get("default", "ignore")


def _normalize_code(value, phase):
    if value is None:
        return PAM_SYSTEM_ERR
    if isinstance(value, bool):
        return PAM_SUCCESS if value else PHASE_FAILURE[phase]
    if isinstance(value, int):
        return _PAM_NUMBERS.get(value, PAM_SYSTEM_ERR)
    if isinstance(value, str):
        normalized = value.strip()
        if normalized.lower() in {"success", "pam_success", "ok", "true"}:
            return PAM_SUCCESS
        if normalized.lower() in {"failure", "false", "fail"}:
            return PHASE_FAILURE[phase]
        normalized = normalized.upper()
        if normalized == PAM_SUCCESS:
            return PAM_SUCCESS
        if normalized.startswith("PAM_"):
            return normalized
        return PAM_SYSTEM_ERR
    return PAM_SYSTEM_ERR


def _phase_value(value, phase):
    if not isinstance(value, dict):
        return value, {}

    env = value.get("env", {})
    if not isinstance(env, dict):
        env = {}

    candidate = None
    if phase == "session":
        for key in ("session", "open"):
            if key in value:
                candidate = value[key]
                break
    if candidate is None and phase in value:
        candidate = value[phase]
    if candidate is None and "result" in value:
        candidate = value["result"]
    if candidate is None and not any(
        key in value for key in PHASES + ("open", "result")
    ):
        candidate = True

    if isinstance(candidate, dict):
        nested_env = candidate.get("env", {})
        if isinstance(nested_env, dict):
            merged = dict(env)
            merged.update(nested_env)
            env = merged
        for key in ("result", phase, "open", "success"):
            if key in candidate:
                candidate = candidate[key]
                break
        else:
            candidate = True
    return candidate, env


def _invoke_callable(function, context):
    try:
        signature = inspect.signature(function)
    except (TypeError, ValueError):
        signature = None
    if signature is None:
        return function()

    kwargs = {}
    for name, parameter in signature.parameters.items():
        if parameter.kind in (
            inspect.Parameter.VAR_POSITIONAL,
            inspect.Parameter.VAR_KEYWORD,
        ):
            continue
        if name in context:
            kwargs[name] = context[name]
    if any(
        parameter.kind == inspect.Parameter.VAR_KEYWORD
        for parameter in signature.parameters.values()
    ):
        kwargs = dict(context)
    return function(**kwargs)


def _handler_result(handler, context):
    if callable(handler):
        return _invoke_callable(handler, context)
    return handler


def _normalize_handler_value(value, phase):
    value, env = _phase_value(value, phase)
    if callable(value):
        value = _handler_result(value, {})
        value, extra_env = _phase_value(value, phase)
        merged = dict(env)
        merged.update(extra_env)
        env = merged
    return _normalize_code(value, phase), value, env


class PamStackEvaluator:
    def __init__(self):
        self._lock = threading.RLock()
        self._configs = {}
        self._versions = {}
        self._audit = []

    def _parse(self, text):
        modules = _parse_text(text)
        _validate_jumps(modules)
        return modules

    def _install(self, service, modules, files):
        with self._lock:
            version = self._versions.get(service, 0) + 1
            snapshot = {
                "service": service,
                "version": version,
                "modules": tuple(copy.deepcopy(modules)),
                "files": tuple(files),
            }
            self._configs[service] = snapshot
            self._versions[service] = version
        return _public_config(snapshot)

    def load_config(self, service, text):
        modules = self._parse(text)
        return self._install(service, modules, ())

    def load_configs(self, service, configs):
        if hasattr(configs, "items"):
            configs = list(configs.items())
        else:
            configs = list(configs)
        modules = []
        files = []
        for name, text in configs:
            files.append(name)
            modules.extend(self._parse(text))
        return self._install(service, modules, files)

    def version(self, service):
        with self._lock:
            return self._versions.get(service, 0)

    def audit_events(self):
        with self._lock:
            return [dict(event) for event in self._audit]

    def _audit_event(self, **event):
        with self._lock:
            self._audit.append(event)

    def evaluate(self, service, user, *, password="", handlers=None):
        handlers = handlers or {}
        with self._lock:
            configs = dict(self._configs)
            snapshot = configs.get(service)

        if snapshot is None or not snapshot["modules"]:
            self._audit_event(
                service=service, user=user, action="evaluate", code=PAM_SYSTEM_ERR
            )
            return {
                "code": PAM_SYSTEM_ERR,
                "trace": [],
                "env": {},
                "closed": [],
                "version": 0 if snapshot is None else snapshot["version"],
            }

        trace = []
        env = {}
        opened = []
        closed = []
        context = {
            "service": service,
            "user": user,
            "password": password,
            "handlers": handlers,
            "configs": configs,
            "trace": trace,
            "env": env,
            "opened": opened,
            "closed": closed,
        }
        code = PAM_SUCCESS
        first_failure = None
        try:
            for phase in PHASES:
                stack = [
                    module
                    for module in snapshot["modules"]
                    if module["type"] == phase
                ]
                if not stack:
                    continue
                if phase == "session" and first_failure is not None:
                    break
                phase_code = self._run_stack(
                    service, phase, stack, context, {service}, 0
                )
                code = phase_code
                if not _is_success_code(phase_code):
                    if first_failure is None:
                        first_failure = phase_code
                    if phase == "auth":
                        break
            if first_failure is not None:
                code = first_failure
        except PamError:
            self._audit_event(
                service=service,
                user=user,
                action="evaluate",
                version=snapshot["version"],
                code=PAM_SYSTEM_ERR,
            )
            raise

        result = {
            "code": code,
            "trace": trace,
            "env": dict(env),
            "closed": list(closed),
            "version": snapshot["version"],
        }
        self._audit_event(
            service=service,
            user=user,
            action="evaluate",
            version=snapshot["version"],
            code=code,
            modules=list(trace),
        )
        return result

    def _stack_for(self, service, phase, context):
        snapshot = context["configs"].get(service)
        if snapshot is None:
            _error("included service not found: %s" % service)
        stack = [
            module
            for module in snapshot["modules"]
            if module["type"] == phase
        ]
        if not stack:
            _error("included stack is empty: %s/%s" % (service, phase))
        return stack

    def _close_opened(self, mark, context):
        opened = context["opened"]
        closed = context["closed"]
        while len(opened) > mark:
            record = opened.pop()
            closed.append(record["module"])
            callback = record.get("close")
            if callback is not None:
                close_context = dict(context)
                close_context.update(
                    {
                        "module": record["module"],
                        "phase": record["phase"],
                        "action": "close",
                    }
                )
                try:
                    _invoke_callable(callback, close_context)
                except Exception:
                    pass

    def _run_stack(self, service, phase, stack, context, active, depth):
        if depth > 128:
            _error("include nesting too deep")

        opened_mark = len(context["opened"])
        required_failed = False
        required_code = None
        success_seen = False
        optional_failed = False
        optional_code = None
        index = 0
        steps = 0
        step_limit = max(100000, (len(stack) + 1) * 1000)

        try:
            while index < len(stack):
                steps += 1
                if steps > step_limit:
                    _error("control flow did not terminate")
                module = stack[index]
                control = module["control"]
                kind = control["kind"]

                if kind in ("include", "substack"):
                    target = module["module"]
                    if target in active:
                        _error("include cycle detected at %s" % target)
                    nested = self._stack_for(target, phase, context)
                    nested_code = self._run_stack(
                        target,
                        phase,
                        nested,
                        context,
                        active | {target},
                        depth + 1,
                    )
                    if not _is_success_code(nested_code):
                        self._close_opened(opened_mark, context)
                        return nested_code
                    success_seen = True
                    index += 1
                    continue

                module_name = module["module"]
                context["trace"].append(module_name)
                handler = context["handlers"].get(module_name)
                handler_context = {
                    "service": service,
                    "user": context["user"],
                    "password": context["password"],
                    "phase": phase,
                    "module": module_name,
                    "args": list(module["args"]),
                    "env": dict(context["env"]),
                    "action": "authenticate",
                }
                raw_value = _handler_result(handler, handler_context)
                code, _, env_updates = _normalize_handler_value(
                    raw_value, phase
                )
                if env_updates:
                    context["env"].update(env_updates)
                success = _is_success_code(code)
                if code == "PAM_IGNORE":
                    index += 1
                    continue

                if phase == "session" and success:
                    close_callback = None
                    candidate = raw_value
                    if isinstance(candidate, dict):
                        close_callback = candidate.get("close")
                        session_value = candidate.get("session")
                        if isinstance(session_value, dict):
                            close_callback = session_value.get(
                                "close", close_callback
                            )
                    if close_callback is None and hasattr(handler, "close"):
                        close_callback = handler.close
                    context["opened"].append(
                        {
                            "module": module_name,
                            "phase": phase,
                            "close": close_callback,
                        }
                    )

                action = None
                if kind == "actions":
                    actions = control["actions"]
                    action = _action_for(actions, code)
                else:
                    action = kind

                if action == "ignore":
                    index += 1
                    continue
                if action == "reset":
                    required_failed = False
                    required_code = None
                    success_seen = False
                    optional_failed = False
                    optional_code = None
                    index += 1
                    continue
                if isinstance(action, int):
                    target = index + 1 + action
                    if target < 0 or target > len(stack):
                        _error("control jump out of bounds")
                    index = target
                    continue
                if action == "die":
                    if not success:
                        self._close_opened(opened_mark, context)
                        return code
                    index += 1
                    continue
                if action == "bad":
                    if not success:
                        if not required_failed:
                            required_failed = True
                            required_code = code
                    index += 1
                    continue
                if action == "ok":
                    if success:
                        success_seen = True
                    index += 1
                    continue
                if action == "done":
                    if success:
                        if required_failed:
                            self._close_opened(opened_mark, context)
                            return required_code
                        return PAM_SUCCESS
                    index += 1
                    continue

                if action == "required":
                    if success:
                        success_seen = True
                    elif not required_failed:
                        required_failed = True
                        required_code = code
                elif action == "requisite":
                    if not success:
                        self._close_opened(opened_mark, context)
                        return code
                    success_seen = True
                elif action == "sufficient":
                    if success:
                        if not required_failed:
                            return PAM_SUCCESS
                        success_seen = True
                    elif not success_seen and not required_failed:
                        optional_failed = True
                        optional_code = code
                elif action == "optional":
                    if success:
                        success_seen = True
                    elif not success_seen and not required_failed:
                        optional_failed = True
                        optional_code = code
                index += 1
        except PamError:
            if phase == "session":
                self._close_opened(opened_mark, context)
            raise

        if required_failed:
            self._close_opened(opened_mark, context)
            return required_code
        if success_seen:
            return PAM_SUCCESS
        if optional_failed:
            self._close_opened(opened_mark, context)
            return optional_code
        self._close_opened(opened_mark, context)
        return PHASE_FAILURE[phase]
