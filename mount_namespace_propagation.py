"""Transactional container mount/propagation guard."""

import os
import posixpath
import threading


class MountPolicyError(Exception):
    def __init__(self, code, message=""):
        self.code = code
        super().__init__(code if not message else "%s: %s" % (code, message))


_VALID_PROPAGATION = ("shared", "slave", "private")


class MountPropagationGuard:
    def __init__(self, max_mounts=1024, allowed_roots=("/srv/volumes",),
                 uid_map=None):
        self.max_mounts = max_mounts
        self.allowed_roots = list(allowed_roots)
        self.uid_map = dict(uid_map or {})
        self._lock = threading.RLock()
        self._mounts = {}
        self._count = 0

    def _normalize_target(self, target):
        if not isinstance(target, str) or not target.startswith("/"):
            raise MountPolicyError("EINVAL", "mount target must be absolute")
        parts = target.split("/")
        if ".." in parts:
            raise MountPolicyError("EINVAL", "mount target must not contain '..'")
        normalized = posixpath.normpath(target)
        if normalized in ("/", "."):
            raise MountPolicyError("EINVAL", "invalid mount target")
        return normalized

    def _resolve_source(self, source, realpath):
        if not isinstance(source, str) or not source.startswith("/"):
            raise MountPolicyError("EINVAL", "mount source must be absolute")
        resolver = realpath if realpath is not None else os.path.realpath
        resolved = resolver(source)
        for root in self.allowed_roots:
            root = root.rstrip("/")
            if resolved == root or resolved.startswith(root + "/"):
                return resolved
        raise MountPolicyError(
            "EACCES", "source %r resolves outside allowed roots" % source)

    def _map_owner(self, owner_uid):
        if owner_uid is None:
            return None
        if self.uid_map:
            if owner_uid not in self.uid_map:
                raise MountPolicyError(
                    "EINVAL", "uid %r not present in user namespace map"
                    % owner_uid)
            return self.uid_map[owner_uid]
        return owner_uid

    def _validate_propagation(self, propagation):
        if propagation not in _VALID_PROPAGATION:
            raise MountPolicyError(
                "EINVAL", "unsupported propagation mode %r" % propagation)
        return propagation

    def _mount_locked(self, container_id, source, target, *, readonly=False,
                      propagation="private", owner_uid=None, realpath=None):
        normalized = self._normalize_target(target)
        resolved = self._resolve_source(source, realpath)
        propagation = self._validate_propagation(propagation)
        host_uid = self._map_owner(owner_uid)
        if self._count >= self.max_mounts:
            raise MountPolicyError("ENOSPC", "mount table limit reached")
        table = self._mounts.setdefault(container_id, {})
        if normalized in table:
            raise MountPolicyError(
                "EINVAL", "target %r already mounted" % normalized)
        record = {
            "source": source,
            "resolved_source": resolved,
            "target": normalized,
            "readonly": bool(readonly),
            "propagation": propagation,
            "owner_uid": owner_uid,
            "host_uid": host_uid,
        }
        table[normalized] = record
        self._count += 1
        return dict(record)

    def create_container(self, container_id, mounts):
        applied = []
        with self._lock:
            try:
                for spec in mounts:
                    spec = dict(spec)
                    target = spec.pop("target")
                    source = spec.pop("source")
                    applied.append(
                        self._mount_locked(container_id, source, target,
                                           **spec))
            except Exception:
                table = self._mounts.get(container_id, {})
                for record in applied:
                    if table.pop(record["target"], None) is not None:
                        self._count -= 1
                if not table:
                    self._mounts.pop(container_id, None)
                raise
        return [dict(record) for record in applied]

    def mount(self, container_id, source, target, *, readonly=False,
              propagation="private", owner_uid=None, realpath=None):
        with self._lock:
            return self._mount_locked(
                container_id, source, target, readonly=readonly,
                propagation=propagation, owner_uid=owner_uid,
                realpath=realpath)

    def remount(self, container_id, target, *, readonly=False):
        with self._lock:
            normalized = self._normalize_target(target)
            record = self._mounts.get(container_id, {}).get(normalized)
            if record is None:
                raise MountPolicyError(
                    "ENOENT", "no mount at %r" % normalized)
            if record["readonly"] and not readonly:
                raise MountPolicyError(
                    "EPERM", "cannot remount readonly mount read-write")
            record["readonly"] = bool(readonly)
            return dict(record)

    def umount(self, container_id, target, *, busy=False):
        with self._lock:
            normalized = self._normalize_target(target)
            table = self._mounts.get(container_id, {})
            if normalized not in table:
                raise MountPolicyError(
                    "ENOENT", "no mount at %r" % normalized)
            if busy:
                raise MountPolicyError(
                    "EBUSY", "target %r is busy" % normalized)
            del table[normalized]
            self._count -= 1
            if not table:
                self._mounts.pop(container_id, None)

    def remove_container(self, container_id):
        with self._lock:
            table = self._mounts.pop(container_id, {})
            removed = len(table)
            self._count -= removed
            return removed

    def mounts(self, container_id):
        with self._lock:
            table = self._mounts.get(container_id, {})
            return [dict(record) for record in table.values()]

    def host_visible(self):
        return []

    def mount_count(self):
        with self._lock:
            return self._count

    def snapshot(self):
        with self._lock:
            return {
                "max_mounts": self.max_mounts,
                "allowed_roots": list(self.allowed_roots),
                "uid_map": dict(self.uid_map),
                "mounts": {
                    container_id: [dict(record) for record in table.values()]
                    for container_id, table in self._mounts.items()
                },
            }

    def restore(self, snapshot):
        with self._lock:
            self.max_mounts = snapshot.get("max_mounts", self.max_mounts)
            self.allowed_roots = list(
                snapshot.get("allowed_roots", self.allowed_roots))
            self.uid_map = dict(snapshot.get("uid_map", self.uid_map))
            self._mounts = {}
            self._count = 0
            for container_id, records in snapshot.get("mounts", {}).items():
                table = {}
                for record in records:
                    record = dict(record)
                    table[record["target"]] = record
                    self._count += 1
                if table:
                    self._mounts[container_id] = table
