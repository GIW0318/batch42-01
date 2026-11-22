"""Core-dump filtering, naming and handler policy."""

import errno
import os
import re
import secrets
import stat
import threading
import time


_UNSAFE_NAME = re.compile(r"[^A-Za-z0-9_.-]+")
_REDACTED = b"[REDACTED]"
_DUMP_PREFIX = "core."
_SAFE_PREFIX = "core-safe."


class CoreDumpError(Exception):
    def __init__(self, code, message=""):
        self.code = code
        super().__init__(code if not message else "%s: %s" % (code, message))


class CoreDumpPolicy:
    def __init__(self, core_dir="/var/crash", max_bytes=1 << 30,
                 max_concurrent=4, sensitive_paths=()):
        self.core_dir = os.fspath(core_dir)
        self.max_bytes = max_bytes
        self.max_concurrent = max(1, int(max_concurrent))
        self.sensitive_paths = []
        self._sensitive_dirs = set()
        for path in sensitive_paths:
            if not path:
                continue
            raw_path = os.fspath(path)
            canonical = self._canonical_path(raw_path)
            self.sensitive_paths.append(canonical)
            if raw_path.endswith(os.sep) or os.path.isdir(canonical):
                self._sensitive_dirs.add(canonical)
        self._lock = threading.RLock()
        self._write_slots = threading.BoundedSemaphore(self.max_concurrent)
        self._active_handler = "builtin"
        self._audit = []
        self._created = {}

    def decide(self, process):
        process = process or {}
        path = self._canonical_path(process.get("path", ""))
        markers = self._secret_markers(process)
        sensitive_path = self._matches_sensitive_path(path)
        sensitive = bool(
            process.get("sensitive") or markers or sensitive_path
        )
        if sensitive_path:
            reason = "sensitive_path"
        elif process.get("sensitive"):
            reason = "sensitive_process"
        elif markers:
            reason = "secret_markers"
        else:
            reason = None
        return {
            "pid": process.get("pid"),
            "name": process.get("name"),
            "path": path,
            "dump": process.get("dump", True) is not False,
            "filter": sensitive,
            "sensitive": sensitive,
            "reason": reason,
            "mode": 0o600,
            "handler": self._current_handler(),
        }

    def dump(self, process, memory, *, now=0.0, realpath=None):
        process = process or {}
        decision = self.decide(process)
        size = self._memory_size(memory)
        timestamp = self._timestamp(now)

        if not decision["dump"]:
            return self._record_skip(
                process, decision, size, timestamp, status="skipped_policy"
            )

        if self.max_bytes is not None and size > self.max_bytes:
            return self._record_skip(process, decision, size, timestamp)

        markers = self._secret_markers(process) if decision["filter"] else []
        payload = self._as_bytes(memory)
        redactions = 0
        if decision["filter"]:
            payload, redactions = self._redact(payload, markers)

        if self.max_bytes is not None and len(payload) > self.max_bytes:
            return self._record_skip(process, decision, size, timestamp)

        try:
            with self._write_slots:
                path = self._write_artifact(
                    process, payload, timestamp, realpath,
                    prefix=_DUMP_PREFIX, redact_name=markers,
                )
        except CoreDumpError:
            raise
        except OSError as exc:
            fallback = self.handle_failure(
                process, error=str(exc), now=timestamp
            )
            if fallback.get("path"):
                return fallback
            raise CoreDumpError(
                "EIO", "dump failed and safe fallback failed: %s" % exc
            )

        record = self._record(
            process, decision, status="ok", path=path,
            size=size, written=len(payload), redactions=redactions,
            timestamp=timestamp,
        )
        self._append_audit(record)
        return record

    def read_dump(self, path):
        if not path:
            raise CoreDumpError("ENOENT", "no dump path supplied")
        path = os.path.abspath(os.fspath(path))
        try:
            info = os.lstat(path)
        except FileNotFoundError:
            raise CoreDumpError("ENOENT", path)
        if stat.S_ISLNK(info.st_mode):
            raise CoreDumpError("ELOOP", path)

        core_dir = os.path.realpath(self.core_dir)
        resolved = os.path.realpath(path)
        if os.path.dirname(resolved) != core_dir:
            raise CoreDumpError("EACCES", "dump is outside core directory")
        if not stat.S_ISREG(info.st_mode):
            raise CoreDumpError("EINVAL", "dump is not a regular file")

        with open(path, "rb") as handle:
            return handle.read()

    def handle_failure(self, process, *, error=None, now=None):
        process = process or {}
        decision = self.decide(process)
        timestamp = self._timestamp(now)
        detail = "handler_failed" if not error else str(error)
        payload = (
            "core-dump-handler-failed\n"
            "pid=%s\n"
            "name=%s\n"
        ) % (process.get("pid", "unknown"), process.get("name", "unknown"))
        payload = payload.encode("utf-8", "replace")

        try:
            with self._write_slots:
                path = self._write_artifact(
                    process, payload, timestamp, None,
                    prefix=_SAFE_PREFIX,
                    redact_name=self._secret_markers(process),
                )
        except (CoreDumpError, OSError) as exc:
            record = self._record(
                process, decision, status="fallback_failed", path=None,
                size=0, written=0, redactions=0,
                timestamp=timestamp, error="%s: %s" % (detail, exc),
            )
            self._append_audit(record)
            return record

        record = self._record(
            process, decision, status="fallback_safe", path=path,
            size=0, written=len(payload), redactions=0,
            timestamp=timestamp, error=detail,
        )
        self._append_audit(record)
        return record

    def set_handler(self, name):
        with self._lock:
            self._active_handler = None if name is None else str(name)

    def handlers(self):
        with self._lock:
            handler = self._active_handler
        return [] if handler is None else [handler]

    def cleanup(self, *, now=0.0, max_age=86400.0):
        reference = float(now)
        removed = []
        try:
            entries = list(os.scandir(self.core_dir))
        except FileNotFoundError:
            entries = []
        except OSError as exc:
            raise CoreDumpError("EIO", str(exc))

        for entry in entries:
            if not entry.name.startswith("core"):
                continue
            try:
                info = entry.stat(follow_symlinks=False)
            except FileNotFoundError:
                continue
            if stat.S_ISDIR(info.st_mode):
                continue

            with self._lock:
                created = self._created.get(entry.path, info.st_mtime)
            if reference - created <= max_age:
                continue
            try:
                os.unlink(entry.path)
            except FileNotFoundError:
                continue
            with self._lock:
                self._created.pop(entry.path, None)
            removed.append(entry.path)

        if removed:
            self._append_audit({
                "event": "cleanup",
                "status": "ok",
                "timestamp": reference,
                "removed": list(removed),
                "bytes": 0,
                "redactions": 0,
            })
        return removed

    def audit_events(self):
        with self._lock:
            return [dict(event) for event in self._audit]

    def _current_handler(self):
        with self._lock:
            return self._active_handler

    def _record_skip(self, process, decision, size, timestamp,
                     *, status="skipped_oversize"):
        record = self._record(
            process, decision, status=status, path=None,
            size=size, written=0, redactions=0, timestamp=timestamp,
        )
        self._append_audit(record)
        return record

    def _record(self, process, decision, *, status, path, size, written,
                redactions, timestamp, error=None):
        record = {
            "event": "dump",
            "status": status,
            "pid": process.get("pid"),
            "name": process.get("name"),
            "path": path,
            "mode": 0o600,
            "bytes": written,
            "original_bytes": size,
            "redactions": redactions,
            "filter": bool(decision.get("filter")),
            "handled_by": (
                "safe-fallback" if status.startswith("fallback")
                else decision.get("handler")
            ),
            "handler_groups": [],
            "writes": 1 if path else 0,
            "timestamp": timestamp,
        }
        if error is not None:
            record["error"] = error
        return record

    def _append_audit(self, record):
        event = dict(record)
        with self._lock:
            self._audit.append(event)

    def _write_artifact(self, process, payload, timestamp, realpath,
                        *, prefix, redact_name=()):
        self._ensure_core_dir()
        core_real = os.path.realpath(self.core_dir)
        name = self._safe_component(process.get("name", "unknown"))
        pid = self._safe_component(process.get("pid", "unknown"))
        for marker, text in self._text_markers(redact_name):
            if text and text in name:
                name = name.replace(text, "redacted")

        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        flags |= getattr(os, "O_NOFOLLOW", 0)

        for _ in range(64):
            filename = "%s%s.%s.%s.core" % (
                prefix, name, pid, secrets.token_hex(12)
            )
            path = os.path.abspath(os.path.join(self.core_dir, filename))
            resolver = realpath or os.path.realpath
            resolved = os.path.abspath(os.fspath(resolver(path)))
            if os.path.dirname(resolved) != core_real:
                raise CoreDumpError(
                    "ELOOP", "dump path resolves outside core directory"
                )

            try:
                fd = os.open(path, flags, 0o600)
            except FileExistsError:
                continue
            except OSError as exc:
                if exc.errno == errno.ELOOP:
                    raise CoreDumpError("ELOOP", path)
                raise

            try:
                os.fchmod(fd, 0o600)
                info = os.fstat(fd)
                if not stat.S_ISREG(info.st_mode):
                    raise CoreDumpError("EINVAL", "dump is not a regular file")
                view = memoryview(payload)
                while view:
                    written = os.write(fd, view)
                    if written <= 0:
                        raise OSError(errno.EIO, "short core write")
                    view = view[written:]
                os.fsync(fd)
            except BaseException:
                try:
                    os.unlink(path)
                except OSError:
                    pass
                raise
            finally:
                os.close(fd)

            try:
                os.utime(path, (timestamp, timestamp))
            except OSError:
                pass
            with self._lock:
                self._created[path] = timestamp
            return path

        raise CoreDumpError("EEXIST", "could not allocate a unique core path")

    def _ensure_core_dir(self):
        try:
            info = os.lstat(self.core_dir)
        except FileNotFoundError:
            info = None
        except OSError as exc:
            raise CoreDumpError("EIO", str(exc))

        if info is not None:
            if stat.S_ISLNK(info.st_mode):
                raise CoreDumpError("ELOOP", "core directory is a symlink")
            if not stat.S_ISDIR(info.st_mode):
                raise CoreDumpError("ENOTDIR", self.core_dir)
            return
        try:
            os.makedirs(self.core_dir, mode=0o700)
            os.chmod(self.core_dir, 0o700)
        except OSError as exc:
            raise CoreDumpError("EIO", str(exc))

    def _matches_sensitive_path(self, path):
        for entry in self.sensitive_paths:
            if path == entry:
                return True
            if entry in self._sensitive_dirs:
                prefix = entry if entry.endswith(os.sep) else entry + os.sep
                if path.startswith(prefix):
                    return True
        return False

    @staticmethod
    def _canonical_path(path):
        if not path:
            return ""
        return os.path.realpath(os.path.normpath(os.path.expanduser(
            os.fspath(path)
        )))

    @staticmethod
    def _timestamp(value):
        return time.time() if value is None else float(value)

    @staticmethod
    def _memory_size(memory):
        try:
            return len(memory)
        except TypeError:
            raise CoreDumpError("EINVAL", "memory must be bytes-like")

    @staticmethod
    def _as_bytes(memory):
        if isinstance(memory, bytes):
            return memory
        if isinstance(memory, str):
            return memory.encode("utf-8")
        try:
            return bytes(memory)
        except TypeError:
            raise CoreDumpError("EINVAL", "memory must be bytes-like")

    @staticmethod
    def _secret_markers(process):
        markers = process.get("secret_markers") or ()
        if isinstance(markers, (bytes, bytearray, memoryview, str)):
            markers = (markers,)
        result = []
        seen = set()
        for marker in markers:
            if isinstance(marker, str):
                marker = marker.encode("utf-8")
            elif isinstance(marker, (bytearray, memoryview)):
                marker = bytes(marker)
            elif not isinstance(marker, bytes):
                continue
            if marker and marker not in seen:
                seen.add(marker)
                result.append(marker)
        return result

    @staticmethod
    def _text_markers(markers):
        result = []
        for marker in markers:
            try:
                text = marker.decode("utf-8")
            except UnicodeDecodeError:
                text = ""
            result.append((marker, text))
        return result

    @staticmethod
    def _redact(payload, markers):
        if not markers:
            return payload, 0
        redactions = 0
        for marker in sorted(markers, key=len, reverse=True):
            count = payload.count(marker)
            if count:
                redactions += count
                payload = payload.replace(marker, _REDACTED)

        # The replacement marker itself can contain a configured secret.
        # Remove any remaining occurrences, including ones formed at splice
        # boundaries, so the caller can verify the result byte-for-byte.
        for marker in markers:
            while marker in payload:
                redactions += payload.count(marker)
                payload = payload.replace(marker, b"")
        return payload, redactions

    @staticmethod
    def _safe_component(value):
        text = _UNSAFE_NAME.sub("_", str(value))
        text = text.strip(".") or "unknown"
        return text[:80]
