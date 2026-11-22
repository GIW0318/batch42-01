import copy
import json
import re
import threading
import time


class ChatError(Exception):
    def __init__(self, code, message=""):
        super().__init__(message or code)
        self.code = code
        self.message = message or code


class ChannelFanout:
    _ADMIN_ROLES = {
        "admin",
        "administrator",
        "gm",
        "moderator",
        "mod",
        "owner",
    }

    def __init__(self, max_length=512, **kwargs):
        self.max_length = max_length
        self.history_retention_ms = kwargs.pop(
            "history_retention_ms", kwargs.pop("retention_ms", 60 * 60 * 1000)
        )
        self._clock = kwargs.pop("clock", None)
        self._channels = {}
        self._connections = {}
        self._subscriptions = {}
        self._messages = {}
        self._requests = {}
        self._muted = {}
        self._blocks = {}
        self._names = {}
        self._filter = []
        self._filter_replacement = "*"
        self._last_delivered = {}
        self._delivered_seqs = {}
        self._subscription_start = {}
        self._channel_seq = {}
        self._lock = threading.RLock()

    def create_channel(self, channel_id, kind="public", min_level=0, **kwargs):
        with self._lock:
            if channel_id in self._channels:
                return None
            allowed_accounts = kwargs.pop(
                "allowed_accounts",
                kwargs.pop(
                    "members",
                    kwargs.pop("whitelist", kwargs.pop("allowed", ())),
                ),
            )
            allowed_roles = kwargs.pop(
                "allowed_roles",
                kwargs.pop("roles", kwargs.pop("whitelist_roles", ())),
            )
            self._channels[channel_id] = {
                "channel_id": channel_id,
                "kind": kind,
                "min_level": int(min_level or 0),
                "allowed_accounts": set(allowed_accounts or ()),
                "allowed_roles": set(allowed_roles or ()),
                "admin_bypass": bool(kwargs.pop("admin_bypass", True)),
                **kwargs,
            }
            self._messages.setdefault(channel_id, {})
            self._channel_seq.setdefault(channel_id, 0)
            return None

    def connect(
        self,
        conn_id,
        account_id,
        server="s1",
        level=0,
        role=None,
        is_admin=False,
        **kwargs,
    ):
        with self._lock:
            roles = kwargs.pop("roles", ())
            if isinstance(roles, str):
                roles = (roles,)
            self._connections[conn_id] = {
                "conn_id": conn_id,
                "account_id": account_id,
                "server": server,
                "level": int(level or 0),
                "role": role,
                "roles": set(roles or ()),
                "is_admin": bool(is_admin),
                "permissions": kwargs.pop("permissions", ()),
                **kwargs,
            }
            self._subscriptions[conn_id] = set()
            for store in (
                self._subscription_start,
                self._delivered_seqs,
                self._last_delivered,
            ):
                for key in list(store):
                    if key[0] == conn_id:
                        store.pop(key, None)
            return None

    def subscribe(self, conn_id, channel_id):
        with self._lock:
            self._require_connection(conn_id)
            self._require_channel(channel_id)
            key = (conn_id, channel_id)
            if key not in self._subscription_start:
                self._subscription_start[key] = self._channel_seq.get(channel_id, 0)
            self._delivered_seqs.setdefault(key, set())
            self._last_delivered.setdefault(
                key, self._subscription_start.get(key, 0)
            )
            self._subscriptions.setdefault(conn_id, set()).add(channel_id)
            return None

    def unsubscribe(self, conn_id, channel_id):
        with self._lock:
            self._require_connection(conn_id)
            self._subscriptions.setdefault(conn_id, set()).discard(channel_id)
            key = (conn_id, channel_id)
            self._subscription_start.pop(key, None)
            self._delivered_seqs.pop(key, None)
            self._last_delivered.pop(key, None)
            return None

    def switch(self, conn_id, channel_id):
        with self._lock:
            self._require_connection(conn_id)
            self._require_channel(channel_id)
            for old_channel in list(self._subscriptions.get(conn_id, ())):
                self.unsubscribe(conn_id, old_channel)
            self.subscribe(conn_id, channel_id)
            return None

    def send(self, request_id, conn_id, channel_id, text, level=None, **kwargs):
        with self._lock:
            connection = self._require_connection(conn_id)
            self._require_channel(channel_id)

            if request_id is not None and request_id in self._requests:
                stored = self._request_entry(request_id)
                if stored is not None:
                    return self._materialize(stored)

            account_id = connection["account_id"]
            if self._is_muted(account_id):
                raise ChatError("MUTED", "account is muted")

            role = kwargs.pop("role", connection.get("role"))
            is_admin = bool(
                kwargs.pop("is_admin", connection.get("is_admin"))
                or role in self._ADMIN_ROLES
                or bool(set(connection.get("roles") or ()) & self._ADMIN_ROLES)
            )
            channel = self._channels[channel_id]
            allowed = (
                (is_admin and channel.get("admin_bypass", True))
                or account_id in channel.get("allowed_accounts", ())
                or role in channel.get("allowed_roles", ())
            )
            effective_level = (
                int(level)
                if level is not None
                else int(connection.get("level", 0) or 0)
            )
            if not allowed and effective_level < channel.get("min_level", 0):
                raise ChatError("FORBIDDEN", "not allowed to speak in channel")

            filtered = self._filter_text("" if text is None else str(text))
            if self._too_long(filtered):
                raise ChatError("TOO_LONG", "message is too long")

            seq = self._next_seq(channel_id)
            entry = self._make_entry(
                channel_id=channel_id,
                seq=seq,
                text=filtered,
                author_id=account_id,
                request_id=request_id,
                source=connection.get("server", "local"),
            )
            self._store_entry(entry)
            if request_id is not None:
                self._requests[request_id] = (channel_id, seq)
            return self._materialize(entry)

    def ingest(self, source, seq, channel_id, text, author_id, request_id=None):
        with self._lock:
            self._require_channel(channel_id)
            try:
                seq = int(seq)
            except (TypeError, ValueError):
                raise ChatError("BAD_SEQUENCE", "sequence must be an integer")
            if seq <= 0:
                raise ChatError("BAD_SEQUENCE", "sequence must be positive")

            if request_id is not None and request_id in self._requests:
                stored = self._request_entry(request_id)
                return None if stored is None else self._materialize(stored)
            if seq in self._messages.get(channel_id, {}):
                return self._materialize(self._messages[channel_id][seq])

            filtered = self._filter_text("" if text is None else str(text))
            if self._too_long(filtered):
                raise ChatError("TOO_LONG", "message is too long")

            entry = self._make_entry(
                channel_id=channel_id,
                seq=seq,
                text=filtered,
                author_id=author_id,
                request_id=request_id,
                source=source,
            )
            self._store_entry(entry)
            if request_id is not None:
                self._requests[request_id] = (channel_id, seq)
            return self._materialize(entry)

    def deliver(self, conn_id):
        with self._lock:
            connection = self._require_connection(conn_id)
            receiver = connection["account_id"]
            deliveries = []

            subscribed = self._subscriptions.get(conn_id, ())
            for channel_id in list(self._channels):
                if channel_id not in subscribed:
                    continue
                key = (conn_id, channel_id)
                start_seq = self._subscription_start.get(key, 0)
                delivered = self._delivered_seqs.setdefault(key, set())
                entries = self._messages.get(channel_id, {})
                pending = [
                    entry
                    for seq, entry in entries.items()
                    if seq > start_seq and seq not in delivered
                ]
                pending.sort(key=lambda item: item["seq"])

                for entry in pending:
                    delivered.add(entry["seq"])
                    self._last_delivered[key] = max(
                        self._last_delivered.get(key, 0), entry["seq"]
                    )
                    if (
                        entry["author_id"] == receiver
                        or not self._is_blocked(receiver, entry["author_id"])
                    ):
                        deliveries.append(self._materialize(entry))

            return deliveries

    def backfill(self, conn_id, channel_id, since_seq):
        with self._lock:
            self._require_connection(conn_id)
            self._require_channel(channel_id)
            try:
                since_seq = int(since_seq or 0)
            except (TypeError, ValueError):
                since_seq = 0

            key = (conn_id, channel_id)
            delivered = self._delivered_seqs.setdefault(key, set())
            entries = self._messages.get(channel_id, {})
            pending = [
                entry
                for seq, entry in entries.items()
                if seq > since_seq and seq not in delivered
            ]
            pending.sort(key=lambda item: item["seq"])

            result = []
            account_id = self._connections[conn_id]["account_id"]
            for entry in pending:
                delivered.add(entry["seq"])
                self._last_delivered[key] = max(
                    self._last_delivered.get(key, 0), entry["seq"]
                )
                if (
                    entry["author_id"] == account_id
                    or not self._is_blocked(account_id, entry["author_id"])
                ):
                    result.append(self._materialize(entry))
            return result

    def mute(self, account_id, until_ms):
        with self._lock:
            if until_ms is None or float(until_ms) <= 0:
                self._muted.pop(account_id, None)
            else:
                self._muted[account_id] = float(until_ms)
            return None

    def block(self, account_id, blocked_id):
        with self._lock:
            self._blocks.setdefault(account_id, set()).add(blocked_id)
            return None

    def rename(self, account_id, new_name):
        with self._lock:
            self._names[account_id] = new_name
            return None

    def name_of(self, account_id):
        with self._lock:
            return self._names.get(account_id, account_id)

    def set_filter(self, words, replacement=None):
        with self._lock:
            if words is None:
                words = ()
            if isinstance(words, str):
                words = (words,)
            self._filter = [str(word) for word in words if str(word)]
            if replacement is not None:
                self._filter_replacement = str(replacement)
            return None

    def snapshot(self):
        with self._lock:
            self._prune_history()
            channels = copy.deepcopy(self._channels)
            for channel in channels.values():
                channel["allowed_accounts"] = sorted(
                    channel.get("allowed_accounts", ()), key=str
                )
                channel["allowed_roles"] = sorted(
                    channel.get("allowed_roles", ()), key=str
                )
            connections = copy.deepcopy(self._connections)
            for connection in connections.values():
                connection["roles"] = sorted(
                    connection.get("roles", ()), key=str
                )
                permissions = connection.get("permissions", ())
                if isinstance(permissions, (set, frozenset)):
                    connection["permissions"] = sorted(permissions, key=str)
            return {
                "version": 1,
                "max_length": self.max_length,
                "history_retention_ms": self.history_retention_ms,
                "channels": channels,
                "connections": connections,
                "subscriptions": {
                    conn: sorted(channels, key=str)
                    for conn, channels in self._subscriptions.items()
                },
                "messages": copy.deepcopy(self._messages),
                "requests": [
                    [request_id, channel_id, seq]
                    for request_id, (channel_id, seq) in self._requests.items()
                ],
                "muted": copy.deepcopy(self._muted),
                "blocks": {
                    account: sorted(blocked, key=str)
                    for account, blocked in self._blocks.items()
                },
                "names": copy.deepcopy(self._names),
                "filter": list(self._filter),
                "filter_replacement": self._filter_replacement,
                "channel_seq": copy.deepcopy(self._channel_seq),
                "last_delivered": [
                    [conn, channel, seq]
                    for (conn, channel), seq in self._last_delivered.items()
                ],
                "delivered_seqs": [
                    [conn, channel, sorted(seqs)]
                    for (conn, channel), seqs in self._delivered_seqs.items()
                ],
                "subscription_start": [
                    [conn, channel, seq]
                    for (conn, channel), seq in self._subscription_start.items()
                ],
            }

    def restore(self, snapshot):
        with self._lock:
            if isinstance(snapshot, str):
                snapshot = json.loads(snapshot)
            if not isinstance(snapshot, dict):
                raise ChatError("BAD_SNAPSHOT", "snapshot must be a mapping")

            self.max_length = snapshot.get("max_length", self.max_length)
            self.history_retention_ms = snapshot.get(
                "history_retention_ms", self.history_retention_ms
            )
            self._channels = self._restore_channels(snapshot.get("channels", {}))
            self._connections = self._restore_connections(
                snapshot.get("connections", {})
            )
            self._messages = self._restore_messages(snapshot.get("messages", {}))
            self._channel_seq = {
                channel: self._max_seq(entries)
                for channel, entries in self._messages.items()
            }
            for channel, seq in snapshot.get("channel_seq", {}).items():
                if channel in self._channels:
                    self._channel_seq[channel] = max(
                        self._channel_seq.get(channel, 0), int(seq)
                    )
            for channel in self._channels:
                self._messages.setdefault(channel, {})
                self._channel_seq.setdefault(channel, 0)

            self._requests = {}
            for item in snapshot.get("requests", ()):
                try:
                    request_id, channel_id, seq = item
                except (TypeError, ValueError):
                    continue
                self._requests[request_id] = (channel_id, int(seq))

            self._muted = {
                account: float(until)
                for account, until in snapshot.get("muted", {}).items()
            }
            self._blocks = {
                account: set(blocked)
                for account, blocked in snapshot.get("blocks", {}).items()
            }
            self._names = dict(snapshot.get("names", {}))
            self._filter = list(snapshot.get("filter", ()))
            self._filter_replacement = snapshot.get("filter_replacement", "*")

            self._subscriptions = {
                conn: set(channels)
                for conn, channels in snapshot.get("subscriptions", {}).items()
                if conn in self._connections
            }
            for conn in self._connections:
                self._subscriptions.setdefault(conn, set())

            self._last_delivered = {}
            for item in snapshot.get("last_delivered", ()):
                try:
                    conn, channel, seq = item
                except (TypeError, ValueError):
                    continue
                self._last_delivered[(conn, channel)] = int(seq)

            self._delivered_seqs = {}
            for item in snapshot.get("delivered_seqs", ()):
                try:
                    conn, channel, seqs = item
                except (TypeError, ValueError):
                    continue
                self._delivered_seqs[(conn, channel)] = {
                    int(seq) for seq in seqs
                }

            self._subscription_start = {}
            for item in snapshot.get("subscription_start", ()):
                try:
                    conn, channel, seq = item
                except (TypeError, ValueError):
                    continue
                self._subscription_start[(conn, channel)] = int(seq)
            for conn, channels in self._subscriptions.items():
                for channel in channels:
                    key = (conn, channel)
                    self._delivered_seqs.setdefault(key, set())
                    self._subscription_start.setdefault(
                        key, self._channel_seq.get(channel, 0)
                    )
                    self._last_delivered.setdefault(
                        key, self._subscription_start[key]
                    )

            self._prune_history()
            return None

    def _make_entry(
        self, channel_id, seq, text, author_id, request_id, source
    ):
        return {
            "seq": seq,
            "channel_id": channel_id,
            "text": text,
            "author_id": author_id,
            "request_id": request_id,
            "source": source,
            "ts_ms": self._now_ms(),
        }

    def _store_entry(self, entry):
        channel_id = entry["channel_id"]
        self._messages.setdefault(channel_id, {})[entry["seq"]] = entry
        self._channel_seq[channel_id] = max(
            self._channel_seq.get(channel_id, 0), entry["seq"]
        )

    def _next_seq(self, channel_id):
        seq = self._channel_seq.get(channel_id, 0) + 1
        while seq in self._messages.get(channel_id, {}):
            seq += 1
        self._channel_seq[channel_id] = seq
        return seq

    def _request_entry(self, request_id):
        target = self._requests.get(request_id)
        if target is None:
            return None
        channel_id, seq = target
        return self._messages.get(channel_id, {}).get(seq)

    def _materialize(self, entry):
        name = self.name_of(entry["author_id"])
        text = entry["text"]
        packets = self._split(text)
        result = dict(entry)
        result.update(
            {
                "author_name": name,
                "author": name,
                "name": name,
                "message": text,
                "packets": packets,
                "fragments": list(packets),
                "chunks": list(packets),
            }
        )
        return result

    def _split(self, text):
        if self.max_length is None or self.max_length <= 0:
            return [text]
        return [
            text[index:index + self.max_length]
            for index in range(0, len(text), self.max_length)
        ] or [""]

    def _filter_text(self, text):
        if not self._filter:
            return text
        pattern = "|".join(
            re.escape(word)
            for word in sorted(self._filter, key=len, reverse=True)
        )
        return re.sub(
            pattern,
            lambda match: self._filter_replacement or "",
            text,
            flags=re.IGNORECASE,
        )

    def _too_long(self, text):
        return self.max_length is not None and len(text) > self.max_length

    def _is_muted(self, account_id):
        until = self._muted.get(account_id)
        if until is None or until <= 0:
            return False
        if until <= 10 ** 12:
            return until > time.time()
        return until > time.time() * 1000

    def _is_blocked(self, receiver_id, author_id):
        return author_id in self._blocks.get(receiver_id, ())

    def _require_connection(self, conn_id):
        try:
            return self._connections[conn_id]
        except KeyError:
            raise ChatError("NO_CONNECTION", "connection does not exist")

    def _require_channel(self, channel_id):
        try:
            return self._channels[channel_id]
        except KeyError:
            raise ChatError("NO_CHANNEL", "channel does not exist")

    def _now_ms(self):
        if self._clock is None:
            return int(time.time() * 1000)
        if callable(self._clock):
            value = self._clock()
        else:
            value = self._clock
        value = float(value)
        return int(value * 1000) if value < 10 ** 11 else int(value)

    def _prune_history(self):
        retention = self.history_retention_ms
        if retention is None or retention <= 0:
            return
        cutoff = self._now_ms() - int(retention)
        for entries in self._messages.values():
            stale = [
                seq
                for seq, entry in entries.items()
                if int(entry.get("ts_ms", cutoff)) < cutoff
            ]
            for seq in stale:
                entries.pop(seq, None)

    def _restore_channels(self, channels):
        restored = {}
        for channel_id, channel in channels.items():
            if not isinstance(channel, dict):
                continue
            item = dict(channel)
            item["channel_id"] = item.get("channel_id", channel_id)
            item["kind"] = item.get("kind", "public")
            item["min_level"] = int(item.get("min_level", 0) or 0)
            item["allowed_accounts"] = set(item.get("allowed_accounts", ()))
            item["allowed_roles"] = set(item.get("allowed_roles", ()))
            item["admin_bypass"] = bool(item.get("admin_bypass", True))
            restored[channel_id] = item
        return restored

    def _restore_connections(self, connections):
        restored = {}
        for conn_id, connection in connections.items():
            if not isinstance(connection, dict):
                continue
            item = dict(connection)
            item["conn_id"] = conn_id
            item.setdefault("account_id", conn_id)
            item.setdefault("server", "s1")
            item["level"] = int(item.get("level", 0) or 0)
            item["roles"] = set(item.get("roles", ()))
            item["is_admin"] = bool(item.get("is_admin", False))
            restored[conn_id] = item
        return restored

    def _restore_messages(self, messages):
        restored = {}
        for channel_id, entries in messages.items():
            if isinstance(entries, dict):
                restored[channel_id] = {
                    int(seq): dict(entry)
                    for seq, entry in entries.items()
                    if isinstance(entry, dict)
                }
            else:
                restored[channel_id] = {
                    int(entry["seq"]): dict(entry)
                    for entry in entries
                    if isinstance(entry, dict) and "seq" in entry
                }
        return restored

    def _max_seq(self, entries):
        return max((int(seq) for seq in entries), default=0)
