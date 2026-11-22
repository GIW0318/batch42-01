"""SSH port/agent forwarding policy gate.

All forwarding decisions are made at channel-open time, before any channel
identifier or file descriptor is handed out, so a denied open can never leak
resources. Hostnames are resolved again at connect time so DNS rebinding
cannot reuse an authorization decision taken against a stale answer.
"""

import ipaddress
import socket
import uuid


ADMIN_PROHIBITED = "SSH_OPEN_ADMINISTRATIVELY_PROHIBITED"
LIMIT_EXCEEDED = "ERESOURCE"
CHANNEL_CLOSED = "CHANNEL_CLOSED"

LOOPBACK_ALIAS = "127.0.0.1"


class ForwardPolicyError(Exception):
    def __init__(self, code, message=""):
        self.code = code
        super().__init__(code if not message else "%s: %s" % (code, message))


def _deny(code=ADMIN_PROHIBITED, message=""):
    raise ForwardPolicyError(code, message)


def normalize_host(host):
    """Canonicalize loopback / wildcard address variants.

    Every IPv4 127/8 address, the unspecified addresses (0.0.0.0, ::) and
    the IPv6 loopback (::1) collapse to a single token so they cannot be
    used to slip past a literal-string blacklist.
    """
    if host is None:
        return None
    text = str(host).strip().lower()
    try:
        parsed = ipaddress.ip_address(text)
    except ValueError:
        return text
    if parsed.is_loopback or parsed.is_unspecified:
        return LOOPBACK_ALIAS
    return str(parsed)


def _default_resolver(host):
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as exc:
        _deny(message="cannot resolve host %r: %s" % (host, exc))
    return infos[0][4][0]


class ForwardPolicyGate:
    def __init__(self, policy=None):
        self.policy = dict(policy or {})
        self._channels = {}
        self._user_channels = {}
        self._audit = []

    def set_policy(self, policy):
        self.policy = dict(policy or {})

    def open_channel(self, user, kind, dest_host=None, dest_port=None, *,
                     bind_addr=None, socket_path=None, groups=(),
                     resolver=None):
        groups = tuple(groups)
        try:
            normalized_host = self._evaluate(
                user, kind, dest_host, bind_addr, socket_path, groups,
                resolver,
            )
            count = self.channel_count(user)
            if count >= int(self.policy.get("max_per_user", 0)):
                _deny(
                    LIMIT_EXCEEDED,
                    "user %r already holds %d forwarding channels"
                    % (user, count),
                )
        except ForwardPolicyError as error:
            self._audit.append({
                "user": user,
                "kind": kind,
                "host": dest_host,
                "port": dest_port,
                "bind_addr": bind_addr,
                "socket_path": socket_path,
                "groups": list(groups),
                "decision": "deny",
                "reason": error.code,
            })
            raise

        channel_id = uuid.uuid4().hex
        record = {
            "id": channel_id,
            "user": user,
            "kind": kind,
            "host": normalized_host,
            "raw_host": dest_host,
            "port": dest_port,
            "bind_addr": bind_addr,
            "socket_path": socket_path,
            "groups": list(groups),
        }
        self._channels[channel_id] = record
        self._user_channels.setdefault(user, set()).add(channel_id)
        self._audit.append({
            "user": user,
            "kind": kind,
            "host": dest_host,
            "port": dest_port,
            "bind_addr": bind_addr,
            "socket_path": socket_path,
            "groups": list(groups),
            "decision": "allow",
            "channel_id": channel_id,
        })
        return dict(record)

    def data(self, channel_id, payload):
        if channel_id not in self._channels:
            _deny(CHANNEL_CLOSED, "channel %r is not open" % channel_id)
        return len(payload)

    def close_channel(self, channel_id):
        record = self._channels.pop(channel_id, None)
        if record is None:
            return False
        owned = self._user_channels.get(record["user"])
        if owned is not None:
            owned.discard(channel_id)
            if not owned:
                del self._user_channels[record["user"]]
        self._audit.append({
            "user": record["user"],
            "kind": record["kind"],
            "host": record["raw_host"],
            "port": record["port"],
            "bind_addr": record["bind_addr"],
            "socket_path": record["socket_path"],
            "groups": record["groups"],
            "decision": "close",
            "channel_id": channel_id,
        })
        return True

    def reload_policy(self, policy):
        self.set_policy(policy)
        closed = []
        for channel_id in list(self._channels):
            record = self._channels[channel_id]
            try:
                self._evaluate(
                    record["user"],
                    record["kind"],
                    record["host"],
                    record["bind_addr"],
                    record["socket_path"],
                    record["groups"],
                    resolver=None,
                )
            except ForwardPolicyError:
                self.close_channel(channel_id)
                closed.append(channel_id)
        return closed

    def channel_count(self, user):
        return len(self._user_channels.get(user, ()))

    def open_fd_count(self):
        return len(self._channels)

    def snapshot_sessions(self):
        return [dict(record) for record in self._channels.values()]

    def restore_sessions(self, snapshot):
        closed = []
        for record in snapshot:
            channel_id = record["id"]
            try:
                normalized_host = self._evaluate(
                    record["user"],
                    record["kind"],
                    record["host"],
                    record.get("bind_addr"),
                    record.get("socket_path"),
                    record.get("groups", ()),
                    resolver=None,
                )
            except ForwardPolicyError:
                closed.append(channel_id)
                self._audit.append({
                    "user": record["user"],
                    "kind": record["kind"],
                    "host": record.get("raw_host"),
                    "port": record.get("port"),
                    "bind_addr": record.get("bind_addr"),
                    "socket_path": record.get("socket_path"),
                    "groups": record.get("groups", []),
                    "decision": "deny",
                    "reason": "RESTORE_REVALIDATION_FAILED",
                    "channel_id": channel_id,
                })
                continue
            restored = dict(record)
            restored["host"] = normalized_host
            self._channels[channel_id] = restored
            self._user_channels.setdefault(
                record["user"], set()
            ).add(channel_id)
            self._audit.append({
                "user": record["user"],
                "kind": record["kind"],
                "host": record.get("raw_host"),
                "port": record.get("port"),
                "bind_addr": record.get("bind_addr"),
                "socket_path": record.get("socket_path"),
                "groups": record.get("groups", []),
                "decision": "restore",
                "channel_id": channel_id,
            })
        return closed

    def audit_events(self):
        return list(self._audit)

    def _is_admin(self, groups):
        admin_group = self.policy.get("admin_group")
        if not admin_group:
            return False
        return admin_group in tuple(groups)

    def _evaluate(self, user, kind, dest_host, bind_addr, socket_path,
                  groups, resolver):
        if kind not in ("local", "dynamic", "remote", "agent"):
            _deny(message="unknown forwarding kind %r" % (kind,))

        if socket_path is not None:
            if socket_path not in tuple(self.policy.get("allow_unix", ())):
                _deny(message="unix socket %r is not allowed" % socket_path)
            return normalize_host(dest_host) if dest_host else socket_path

        if kind == "dynamic" and not self.policy.get("allow_dynamic", False):
            _deny(message="dynamic (SOCKS) forwarding is disabled")

        if kind == "agent" and not self.policy.get("allow_agent", False):
            _deny(message="agent forwarding is disabled")

        if kind == "remote":
            self._check_remote_bind(bind_addr)

        return self._check_destination(dest_host, groups, resolver)

    def _check_remote_bind(self, bind_addr):
        if bind_addr is not None:
            try:
                parsed_bind = ipaddress.ip_address(str(bind_addr).strip())
            except ValueError:
                _deny(message="invalid remote bind address %r" % bind_addr)
            if parsed_bind.is_unspecified:
                _deny(
                    message="remote forwarding must not bind wildcard "
                            "addresses",
                )
        requested = normalize_host(bind_addr or LOOPBACK_ALIAS)
        allowed_binds = {
            normalize_host(item)
            for item in self.policy.get("allow_remote_bind", ())
        }
        if requested not in allowed_binds:
            _deny(
                message="remote bind address %r is not allowed" % bind_addr,
            )

    def _check_destination(self, dest_host, groups, resolver):
        if dest_host is None:
            _deny(message="forwarding requires a destination host")

        text = str(dest_host).strip().lower()
        try:
            ipaddress.ip_address(text)
        except ValueError:
            return self._check_hostname(text, groups, resolver)

        normalized = normalize_host(text)
        self._check_host_policy(normalized, groups)
        return normalized

    def _check_hostname(self, hostname, groups, resolver):
        resolve = resolver or _default_resolver
        authorized = self._resolve_and_check(hostname, groups, resolve)
        connected = self._resolve_and_check(hostname, groups, resolve)
        if connected != authorized:
            _deny(
                message="DNS rebinding detected: %r now resolves to %r "
                        "(authorized %r)" % (hostname, connected, authorized),
            )
        return connected

    def _resolve_and_check(self, hostname, groups, resolve):
        answer = resolve(hostname)
        if isinstance(answer, (list, tuple)):
            answer = answer[0]
        normalized = normalize_host(answer)
        self._check_host_policy(normalized, groups)
        return normalized

    def _check_host_policy(self, normalized_host, groups):
        if self._is_admin(groups):
            return

        if normalized_host == LOOPBACK_ALIAS and \
                self.policy.get("deny_loopback", False):
            _deny(message="loopback destinations are denied")

        deny_hosts = {
            normalize_host(item) for item in self.policy.get("deny_hosts", ())
        }
        if normalized_host in deny_hosts:
            _deny(message="host %r is denied by policy" % normalized_host)

        allow_hosts = {
            normalize_host(item) for item in self.policy.get("allow_hosts", ())
        }
        if normalized_host not in allow_hosts:
            _deny(message="host %r is not in the allow list" % normalized_host)
