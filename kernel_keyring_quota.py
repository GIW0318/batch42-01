"""Kernel keyring quota, permission and reference accounting broker."""

from __future__ import annotations

import base64
import copy
import threading
import time
from dataclasses import dataclass
from typing import Any


class KeyringError(Exception):
    def __init__(self, code, message=""):
        self.code = code
        super().__init__(code if not message else "%s: %s" % (code, message))


@dataclass
class _Entry:
    serial: int
    uid: int
    keyring: str
    description: str
    payload: bytes
    perms: int
    timeout: float | None
    expires_at: float | None
    refcount: int
    revoked: bool = False


class KeyringQuotaBroker:
    _SESSION_KEYRINGS = frozenset(("session", "thread"))

    def __init__(
        self,
        max_bytes=65536,
        max_entries=1024,
        *,
        max_session_bytes=None,
        max_session_entries=None,
        **kwargs,
    ):
        aliases = {
            "session_max_bytes": "max_session_bytes",
            "max_per_session_bytes": "max_session_bytes",
            "session_bytes": "max_session_bytes",
            "session_max_entries": "max_session_entries",
            "max_per_session_entries": "max_session_entries",
            "session_entries": "max_session_entries",
        }
        for name, value in kwargs.items():
            target = aliases.get(name)
            if target is None:
                raise TypeError("unexpected keyword argument %r" % name)
            if target == "max_session_bytes":
                max_session_bytes = value
            else:
                max_session_entries = value

        self._validate_limit(max_bytes, "max_bytes")
        self._validate_limit(max_entries, "max_entries")
        if max_session_bytes is not None:
            self._validate_limit(max_session_bytes, "max_session_bytes")
        if max_session_entries is not None:
            self._validate_limit(max_session_entries, "max_session_entries")

        self.max_bytes = max_bytes
        self.max_entries = max_entries
        self.max_session_bytes = (
            max_bytes if max_session_bytes is None else max_session_bytes
        )
        self.max_session_entries = (
            max_entries if max_session_entries is None else max_session_entries
        )

        self._lock = threading.RLock()
        self._entries: dict[int, _Entry] = {}
        self._index: dict[tuple[int, str, str], int] = {}
        self._links: dict[int, dict[str, set[str]]] = {}
        self._next_serial = 1
        self._audit: list[dict[str, Any]] = []
        self._stale_sessions: dict[tuple[int, str], set[int]] = {}
        self._uid_transitions: dict[int, set[int]] = {}

    @staticmethod
    def _validate_limit(value, name):
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise KeyringError("EINVAL", "%s must be a non-negative integer" % name)

    @staticmethod
    def _validate_uid(uid):
        if not isinstance(uid, int) or isinstance(uid, bool) or uid < 0:
            raise KeyringError("EINVAL", "uid must be a non-negative integer")

    @staticmethod
    def _validate_name(value, name):
        if not isinstance(value, str) or not value:
            raise KeyringError("EINVAL", "%s must be a non-empty string" % name)
        return value

    @staticmethod
    def _payload_bytes(payload):
        if not isinstance(payload, (bytes, bytearray, memoryview)):
            raise KeyringError("EINVAL", "payload must be bytes-like")
        return bytes(payload)

    @staticmethod
    def _validate_perms(perms):
        if (
            not isinstance(perms, int)
            or isinstance(perms, bool)
            or perms < 0
            or perms > 0o777
        ):
            raise KeyringError("EINVAL", "perms must be within 0000..0777")
        return perms

    def _is_persistent(self, keyring):
        return keyring not in self._SESSION_KEYRINGS

    def _entry_dict(self, entry):
        return {
            "serial": entry.serial,
            "uid": entry.uid,
            "keyring": entry.keyring,
            "description": entry.description,
            "perms": entry.perms,
            "timeout": entry.timeout,
            "expires_at": entry.expires_at,
            "refcount": entry.refcount,
            "revoked": entry.revoked,
        }

    def _audit_locked(self, operation, *, serial=None, uid=None, keyring=None,
                      description=None, code=None):
        event = {
            "seq": len(self._audit) + 1,
            "time": time.time(),
            "operation": operation,
        }
        for name, value in (
            ("serial", serial),
            ("uid", uid),
            ("keyring", keyring),
            ("description", description),
            ("code", code),
        ):
            if value is not None:
                event[name] = value
        self._audit.append(event)
        return event

    def _active_entries_locked(self, uid, keyring=None):
        entries = []
        for entry in self._entries.values():
            if entry.uid != uid or entry.revoked:
                continue
            if keyring is not None and entry.keyring != keyring:
                continue
            entries.append(entry)
        return entries

    def _usage_locked(self, uid, keyring=None, exclude_serial=None):
        entries = self._active_entries_locked(uid, keyring)
        if exclude_serial is not None:
            entries = [
                entry for entry in entries if entry.serial != exclude_serial
            ]
        return len(entries), sum(len(entry.payload) for entry in entries)

    def _check_payload_locked(self, uid, keyring, size, exclude_serial=None):
        if size > self.max_bytes:
            raise KeyringError(
                "E2BIG",
                "payload is %d bytes, entry limit is %d" % (size, self.max_bytes),
            )

        entries, used_bytes = self._usage_locked(uid, exclude_serial=exclude_serial)
        if entries + 1 > self.max_entries or used_bytes + size > self.max_bytes:
            raise KeyringError("EDQUOT", "UID keyring quota exceeded")

        session_entries, session_bytes = self._usage_locked(
            uid, keyring, exclude_serial=exclude_serial
        )
        if (
            session_entries + 1 > self.max_session_entries
            or session_bytes + size > self.max_session_bytes
        ):
            raise KeyringError("EDQUOT", "session keyring quota exceeded")

    def _lookup_locked(self, uid, keyring, description):
        serial = self._index.get((uid, keyring, description))
        if serial is None:
            return None
        return self._entries.get(serial)

    def _check_access_locked(self, entry, requester_uid):
        stale = self._stale_sessions.get((entry.uid, entry.keyring), ())
        transitions = self._uid_transitions.get(entry.uid, ())
        if requester_uid in stale or (
            not self._is_persistent(entry.keyring)
            and requester_uid in transitions
        ):
            raise KeyringError("EACCES", "session keyring is not accessible")

        if requester_uid == entry.uid:
            allowed = bool(entry.perms & 0o400)
        else:
            allowed = bool(entry.perms & 0o004)
        if not allowed:
            raise KeyringError("EACCES", "permission denied")

    @staticmethod
    def _check_expiry_locked(entry, now):
        if entry.expires_at is not None and now >= entry.expires_at:
            raise KeyringError("EKEYEXPIRED", "key has expired")

    def add(self, uid, keyring, description, payload, *, perms=0o600,
            timeout=None, now=0.0):
        self._validate_uid(uid)
        keyring = self._validate_name(keyring, "keyring")
        description = self._validate_name(description, "description")
        payload = self._payload_bytes(payload)
        perms = self._validate_perms(perms)
        if timeout is not None and not isinstance(timeout, (int, float)):
            raise KeyringError("EINVAL", "timeout must be numeric")

        with self._lock:
            existing = self._lookup_locked(uid, keyring, description)
            if existing is not None and not existing.revoked:
                self._audit_locked(
                    "add",
                    serial=existing.serial,
                    uid=uid,
                    keyring=keyring,
                    description=description,
                    code="EEXIST",
                )
                raise KeyringError("EEXIST", "description already exists")

            self._check_payload_locked(uid, keyring, len(payload))

            serial = self._next_serial
            self._next_serial += 1
            expires_at = None
            if timeout is not None:
                expires_at = float(now) + float(timeout)
            entry = _Entry(
                serial=serial,
                uid=uid,
                keyring=keyring,
                description=description,
                payload=payload,
                perms=perms,
                timeout=timeout,
                expires_at=expires_at,
                refcount=2,
            )
            self._entries[serial] = entry
            self._index[(uid, keyring, description)] = serial
            self._audit_locked(
                "add",
                serial=serial,
                uid=uid,
                keyring=keyring,
                description=description,
            )
            return self._entry_dict(entry)

    def read(self, uid, keyring, description, *, requester_uid=None, now=0.0):
        self._validate_uid(uid)
        keyring = self._validate_name(keyring, "keyring")
        description = self._validate_name(description, "description")
        if requester_uid is None:
            requester_uid = uid
        self._validate_uid(requester_uid)

        with self._lock:
            entry = self._lookup_locked(uid, keyring, description)
            if entry is None:
                self._audit_locked(
                    "read",
                    uid=uid,
                    keyring=keyring,
                    description=description,
                    code="ENOENT",
                )
                raise KeyringError("ENOENT", "key not found")

            try:
                self._check_access_locked(entry, requester_uid)
                if entry.revoked:
                    raise KeyringError("EKEYREVOKED", "key has been revoked")
                self._check_expiry_locked(entry, now)
            except KeyringError as exc:
                self._audit_locked(
                    "read",
                    serial=entry.serial,
                    uid=uid,
                    keyring=keyring,
                    description=description,
                    code=exc.code,
                )
                raise

            self._audit_locked(
                "read",
                serial=entry.serial,
                uid=uid,
                keyring=keyring,
                description=description,
            )
            return entry.payload

    def update(self, key_id, payload):
        payload = self._payload_bytes(payload)

        with self._lock:
            entry = self._entries.get(key_id)
            if entry is None:
                raise KeyringError("ENOENT", "key not found")
            if entry.revoked:
                raise KeyringError("EKEYREVOKED", "key has been revoked")

            self._check_payload_locked(
                entry.uid,
                entry.keyring,
                len(payload),
                exclude_serial=entry.serial,
            )
            entry.payload = payload
            self._audit_locked(
                "update",
                serial=entry.serial,
                uid=entry.uid,
                keyring=entry.keyring,
                description=entry.description,
            )
            return self._entry_dict(entry)

    def revoke(self, key_id):
        with self._lock:
            entry = self._entries.get(key_id)
            if entry is None:
                raise KeyringError("ENOENT", "key not found")
            if not entry.revoked:
                entry.revoked = True
                entry.refcount = 0
                entry.payload = b""
                self._audit_locked(
                    "revoke",
                    serial=entry.serial,
                    uid=entry.uid,
                    keyring=entry.keyring,
                    description=entry.description,
                )
            return self._entry_dict(entry)

    def ref(self, key_id):
        with self._lock:
            entry = self._entries.get(key_id)
            if entry is None:
                raise KeyringError("ENOENT", "key not found")
            return entry.refcount

    def unlink(self, key_id):
        with self._lock:
            entry = self._entries.get(key_id)
            if entry is None:
                raise KeyringError("ENOENT", "key not found")
            if entry.revoked or entry.refcount == 0:
                return 0

            entry.refcount -= 1
            if entry.refcount == 0:
                self._forget_entry_locked(entry)
            self._audit_locked(
                "unlink",
                serial=entry.serial,
                uid=entry.uid,
                keyring=entry.keyring,
                description=entry.description,
            )
            return entry.refcount

    def _forget_entry_locked(self, entry):
        self._entries.pop(entry.serial, None)
        index_key = (entry.uid, entry.keyring, entry.description)
        if self._index.get(index_key) == entry.serial:
            self._index.pop(index_key, None)

    def process_exit(self, uid, session):
        self._validate_uid(uid)
        session = self._validate_name(session, "session")

        with self._lock:
            removed = 0
            for entry in list(self._entries.values()):
                if entry.uid == uid and entry.keyring == session:
                    self._forget_entry_locked(entry)
                    removed += 1
            self._stale_sessions.pop((uid, session), None)
            self._audit_locked(
                "process_exit",
                uid=uid,
                keyring=session,
            )
            return removed

    def setuid(self, uid, new_uid):
        self._validate_uid(uid)
        self._validate_uid(new_uid)

        with self._lock:
            keyrings = {
                entry.keyring
                for entry in self._entries.values()
                if entry.uid == uid and not self._is_persistent(entry.keyring)
            }
            keyrings.add("session")
            for keyring in keyrings:
                self._stale_sessions.setdefault((uid, keyring), set()).add(new_uid)
            self._uid_transitions.setdefault(uid, set()).add(new_uid)
            self._audit_locked("setuid", uid=uid, code="EACCES")
            return {"uid": uid, "new_uid": new_uid}

    def link_keyring(self, uid, parent, child):
        self._validate_uid(uid)
        parent = self._validate_name(parent, "parent")
        child = self._validate_name(child, "child")

        with self._lock:
            links = self._links.setdefault(uid, {})
            links.setdefault(parent, set()).add(child)
            links.setdefault(child, set())
            self._audit_locked(
                "link_keyring",
                uid=uid,
                keyring=parent,
                description=child,
            )
            return {"parent": parent, "child": child}

    def _check_cycles_locked(self, uid, root):
        links = self._links.get(uid, {})
        state: dict[str, int] = {}

        def visit(node):
            status = state.get(node, 0)
            if status == 1:
                raise KeyringError("ELOOP", "keyring link cycle detected")
            if status == 2:
                return
            state[node] = 1
            for child in links.get(node, ()):
                visit(child)
            state[node] = 2

        visit(root)

    def search(self, uid, keyring, description):
        self._validate_uid(uid)
        keyring = self._validate_name(keyring, "keyring")
        description = self._validate_name(description, "description")

        with self._lock:
            self._check_cycles_locked(uid, keyring)
            links = self._links.get(uid, {})
            seen = set()
            pending = [keyring]
            matches = []
            while pending:
                current = pending.pop()
                if current in seen:
                    continue
                seen.add(current)
                entry = self._lookup_locked(uid, current, description)
                if entry is not None and not entry.revoked:
                    matches.append(self._entry_dict(entry))
                pending.extend(links.get(current, ()))
            matches.sort(key=lambda item: item["serial"])
            return matches

    def quota(self, uid):
        self._validate_uid(uid)
        with self._lock:
            entries = self._active_entries_locked(uid)
            sessions: dict[str, dict[str, int]] = {}
            total_bytes = 0
            for entry in entries:
                total_bytes += len(entry.payload)
                session = sessions.setdefault(
                    entry.keyring, {"entries": 0, "bytes": 0}
                )
                session["entries"] += 1
                session["bytes"] += len(entry.payload)
            return {
                "uid": uid,
                "entries": len(entries),
                "bytes": total_bytes,
                "sessions": sessions,
                "max_entries": self.max_entries,
                "max_bytes": self.max_bytes,
                "max_session_entries": self.max_session_entries,
                "max_session_bytes": self.max_session_bytes,
            }

    def snapshot(self):
        with self._lock:
            entries = []
            for entry in sorted(self._entries.values(), key=lambda item: item.serial):
                if entry.revoked or not self._is_persistent(entry.keyring):
                    continue
                entries.append(
                    {
                        "serial": entry.serial,
                        "uid": entry.uid,
                        "keyring": entry.keyring,
                        "description": entry.description,
                        "payload_b64": base64.b64encode(entry.payload).decode("ascii"),
                        "perms": entry.perms,
                        "timeout": entry.timeout,
                        "expires_at": entry.expires_at,
                        "refcount": entry.refcount,
                    }
                )
            links = {
                uid: {
                    parent: sorted(children)
                    for parent, children in graph.items()
                }
                for uid, graph in self._links.items()
            }
            return {
                "version": 1,
                "max_bytes": self.max_bytes,
                "max_entries": self.max_entries,
                "max_session_bytes": self.max_session_bytes,
                "max_session_entries": self.max_session_entries,
                "next_serial": self._next_serial,
                "entries": copy.deepcopy(entries),
                "links": copy.deepcopy(links),
            }

    def restore(self, snapshot):
        if not isinstance(snapshot, dict):
            raise KeyringError("EINVAL", "snapshot must be a mapping")

        with self._lock:
            max_bytes = snapshot.get("max_bytes", self.max_bytes)
            max_entries = snapshot.get("max_entries", self.max_entries)
            max_session_bytes = snapshot.get(
                "max_session_bytes", self.max_session_bytes
            )
            max_session_entries = snapshot.get(
                "max_session_entries", self.max_session_entries
            )
            self._validate_limit(max_bytes, "max_bytes")
            self._validate_limit(max_entries, "max_entries")
            self._validate_limit(max_session_bytes, "max_session_bytes")
            self._validate_limit(max_session_entries, "max_session_entries")

            entries: dict[int, _Entry] = {}
            index: dict[tuple[int, str, str], int] = {}
            max_serial = 0
            for raw in snapshot.get("entries", ()):
                try:
                    payload = base64.b64decode(raw["payload_b64"], validate=True)
                    serial = int(raw["serial"])
                    uid = int(raw["uid"])
                    keyring = str(raw["keyring"])
                    description = str(raw["description"])
                    perms = int(raw.get("perms", 0o600))
                    refcount = int(raw.get("refcount", 2))
                except (KeyError, TypeError, ValueError) as exc:
                    raise KeyringError("EINVAL", "invalid snapshot entry") from exc
                self._validate_uid(uid)
                self._validate_name(keyring, "keyring")
                self._validate_name(description, "description")
                self._validate_perms(perms)
                if serial <= 0 or refcount < 0 or serial in entries:
                    raise KeyringError("EINVAL", "invalid snapshot entry")
                if not self._is_persistent(keyring):
                    continue
                entry = _Entry(
                    serial=serial,
                    uid=uid,
                    keyring=keyring,
                    description=description,
                    payload=payload,
                    perms=perms,
                    timeout=raw.get("timeout"),
                    expires_at=raw.get("expires_at"),
                    refcount=refcount,
                )
                entries[serial] = entry
                index[(uid, keyring, description)] = serial
                max_serial = max(max_serial, serial)

            links = {}
            raw_links = snapshot.get("links", {})
            if not isinstance(raw_links, dict):
                raise KeyringError("EINVAL", "invalid snapshot links")
            for raw_uid, graph in raw_links.items():
                uid = int(raw_uid)
                self._validate_uid(uid)
                if not isinstance(graph, dict):
                    raise KeyringError("EINVAL", "invalid snapshot links")
                links[uid] = {
                    str(parent): {str(child) for child in children}
                    for parent, children in graph.items()
                }

            self.max_bytes = max_bytes
            self.max_entries = max_entries
            self.max_session_bytes = max_session_bytes
            self.max_session_entries = max_session_entries
            self._entries = entries
            self._index = index
            self._links = links
            self._stale_sessions = {}
            self._uid_transitions = {}
            self._next_serial = max(
                int(snapshot.get("next_serial", 1)), max_serial + 1
            )
            self._audit_locked("restore")
            return {"entries": len(entries)}

    def audit_events(self):
        with self._lock:
            return copy.deepcopy(self._audit)
