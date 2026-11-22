"""Project quota reservation and accounting.

Each project keeps two independent ledgers (physical blocks and inodes).
Reservations are atomic: both ledgers are validated against the effective
limits before anything is committed, so a rejected reservation never leaves
partial usage behind.  A single reentrant lock makes the check-and-commit
sequence safe under concurrent writers.

Effective limits are the minimum of every limit along the parent chain.
The grace timer of a ledger starts at the first successful over-limit
reservation and is cleared once usage falls back under the limit.  Sparse
files are charged by physical blocks, symlinks consume nothing, and inodes
are deduplicated by inode id so hard links are counted once.
"""

import threading

QUOTA_EXCEEDED = "quota-exceeded"
UNKNOWN_PROJECT = "unknown-project"


class QuotaError(Exception):
    def __init__(self, code, message=""):
        self.code = code
        super().__init__(code if not message else "%s: %s" % (code, message))


class ProjectQuotaEnforcer:
    """Atomic block/inode reservations per project with parent minima.

    Limits are two ledgers (blocks and inodes); both are checked before a
    reservation commits.  The grace period starts at the first over-limit
    attempt.  Sparse files count physical blocks; symlinks consume nothing.
    """

    def __init__(self, block_limit=1000, inode_limit=100, grace_seconds=60):
        self.block_limit = block_limit
        self.inode_limit = inode_limit
        self.grace_seconds = grace_seconds
        self._clock = 0
        self._lock = threading.RLock()
        self._projects = {}
        self._handles = {}
        self._next_handle = 0

    def _get_project(self, project_id):
        project = self._projects.get(project_id)
        if project is None:
            raise QuotaError(UNKNOWN_PROJECT, str(project_id))
        return project

    def _effective_limits(self, project_id):
        """Minimum block/inode limit across the whole parent chain."""
        block_cap = None
        inode_cap = None
        current_id = project_id
        while current_id is not None:
            current = self._projects[current_id]
            if block_cap is None or current["block_limit"] < block_cap:
                block_cap = current["block_limit"]
            if inode_cap is None or current["inode_limit"] < inode_cap:
                inode_cap = current["inode_limit"]
            current_id = current["parent"]
        return block_cap, inode_cap

    def _ledger_admitted(self, project, used, requested, limit, exceed_key):
        """Check one ledger without mutating any state.

        An over-limit request starts the grace timer on commit and is only
        admitted while virtual time is running (clock above zero) and the
        timer has not expired.  Requests at clock zero have no grace window
        available and are rejected like a hard-limit violation.
        """
        if used + requested <= limit:
            return True
        first_exceed = project[exceed_key]
        if first_exceed is None:
            return self._clock > 0
        return self._clock - first_exceed < self.grace_seconds

    def _refresh_grace_timers(self, project, block_cap, inode_cap):
        if project["blocks"] > block_cap and project["block_exceed_at"] is None:
            project["block_exceed_at"] = self._clock
        if project["inodes"] > inode_cap and project["inode_exceed_at"] is None:
            project["inode_exceed_at"] = self._clock
        if project["blocks"] <= block_cap:
            project["block_exceed_at"] = None
        if project["inodes"] <= inode_cap:
            project["inode_exceed_at"] = None

    def create_project(self, project_id, parent=None, block_limit=None,
                       inode_limit=None):
        with self._lock:
            if project_id in self._projects:
                raise QuotaError("project-exists", str(project_id))
            if parent is not None and parent not in self._projects:
                raise QuotaError(UNKNOWN_PROJECT, str(parent))
            self._projects[project_id] = {
                "parent": parent,
                "block_limit": (self.block_limit if block_limit is None
                                else block_limit),
                "inode_limit": (self.inode_limit if inode_limit is None
                                else inode_limit),
                "blocks": 0,
                "inodes": 0,
                "inode_ids": set(),
                "files": {},
                "block_exceed_at": None,
                "inode_exceed_at": None,
            }

    def set_quota(self, project_id, block_limit=None, inode_limit=None):
        with self._lock:
            project = self._get_project(project_id)
            if block_limit is not None:
                project["block_limit"] = block_limit
            if inode_limit is not None:
                project["inode_limit"] = inode_limit
            block_cap, inode_cap = self._effective_limits(project_id)
            self._refresh_grace_timers(project, block_cap, inode_cap)

    def usage(self, project_id):
        with self._lock:
            project = self._get_project(project_id)
            return {"blocks": project["blocks"], "inodes": project["inodes"]}

    def reserve(self, project_id, blocks=0, inodes=0, inode_id=None,
                kind="file", sparse=False, apparent_blocks=None, path=""):
        with self._lock:
            project = self._get_project(project_id)
            if kind == "symlink":
                return
            block_cap, inode_cap = self._effective_limits(project_id)
            inode_known = (inode_id is not None
                           and inode_id in project["inode_ids"])
            new_inodes = 0 if inode_known else inodes
            block_ok = self._ledger_admitted(
                project, project["blocks"], blocks, block_cap,
                "block_exceed_at")
            inode_ok = self._ledger_admitted(
                project, project["inodes"], new_inodes, inode_cap,
                "inode_exceed_at")
            if not block_ok:
                raise QuotaError(
                    QUOTA_EXCEEDED,
                    "block quota exceeded for project %s" % project_id)
            if not inode_ok:
                raise QuotaError(
                    QUOTA_EXCEEDED,
                    "inode quota exceeded for project %s" % project_id)
            project["blocks"] += blocks
            if new_inodes:
                project["inodes"] += new_inodes
                if inode_id is not None:
                    project["inode_ids"].add(inode_id)
            if path:
                project["files"][path] = (
                    project["files"].get(path, 0) + blocks)
            self._refresh_grace_timers(project, block_cap, inode_cap)

    def release(self, project_id, blocks=0, inodes=0, inode_id=None):
        with self._lock:
            project = self._get_project(project_id)
            project["blocks"] = max(0, project["blocks"] - blocks)
            if inodes:
                if inode_id is None or inode_id in project["inode_ids"]:
                    project["inode_ids"].discard(inode_id)
                    project["inodes"] = max(0, project["inodes"] - inodes)
            block_cap, inode_cap = self._effective_limits(project_id)
            self._refresh_grace_timers(project, block_cap, inode_cap)

    def open_file(self, project_id, path):
        with self._lock:
            self._get_project(project_id)
            handle = self._next_handle
            self._next_handle += 1
            self._handles[handle] = {"project": project_id, "path": path}
            return handle

    def write(self, handle, blocks=0, inodes=0):
        with self._lock:
            info = self._handles.get(handle)
            if info is None:
                raise QuotaError("unknown-handle", str(handle))
            self.reserve(info["project"], blocks=blocks, inodes=inodes,
                         path=info["path"])

    def rename(self, src_project, dst_project, path, blocks=0):
        with self._lock:
            self._get_project(src_project)
            self._get_project(dst_project)
            if src_project == dst_project:
                return
            self.reserve(dst_project, blocks=blocks, path=path)
            self.release(src_project, blocks=blocks)

    def truncate(self, project_id, path, new_blocks):
        with self._lock:
            project = self._get_project(project_id)
            current_blocks = project["files"].get(path)
            if current_blocks is None:
                current_blocks = project["blocks"]
            delta = new_blocks - current_blocks
            block_cap, _ = self._effective_limits(project_id)
            if delta > 0 and not self._ledger_admitted(
                    project, project["blocks"], delta, block_cap,
                    "block_exceed_at"):
                raise QuotaError(
                    QUOTA_EXCEEDED,
                    "block quota exceeded while growing %s" % path)
            project["blocks"] = max(0, project["blocks"] + delta)
            project["files"][path] = new_blocks
            inode_cap = self._effective_limits(project_id)[1]
            self._refresh_grace_timers(project, block_cap, inode_cap)

    def advance(self, seconds):
        with self._lock:
            self._clock += seconds

    def snapshot(self):
        with self._lock:
            return {
                "version": 1,
                "clock": self._clock,
                "grace_seconds": self.grace_seconds,
                "projects": {
                    project_id: {
                        "parent": project["parent"],
                        "block_limit": project["block_limit"],
                        "inode_limit": project["inode_limit"],
                        "blocks": project["blocks"],
                        "inodes": project["inodes"],
                        "inode_ids": sorted(project["inode_ids"]),
                        "files": dict(project["files"]),
                        "block_exceed_at": project["block_exceed_at"],
                        "inode_exceed_at": project["inode_exceed_at"],
                    }
                    for project_id, project in self._projects.items()
                },
            }

    def restore(self, snapshot):
        with self._lock:
            self._clock = snapshot.get("clock", 0)
            if "grace_seconds" in snapshot:
                self.grace_seconds = snapshot["grace_seconds"]
            restored = {}
            for project_id, saved in snapshot["projects"].items():
                restored[project_id] = {
                    "parent": saved["parent"],
                    "block_limit": saved["block_limit"],
                    "inode_limit": saved["inode_limit"],
                    "blocks": saved["blocks"],
                    "inodes": saved["inodes"],
                    "inode_ids": set(saved.get("inode_ids", ())),
                    "files": dict(saved.get("files", {})),
                    "block_exceed_at": saved.get("block_exceed_at"),
                    "inode_exceed_at": saved.get("inode_exceed_at"),
                }
            for project_id, project in restored.items():
                if project["parent"] is not None and project["parent"] not in restored:
                    raise QuotaError(UNKNOWN_PROJECT, str(project["parent"]))
                if len(project["inode_ids"]) > project["inodes"]:
                    raise QuotaError(
                        "corrupt-snapshot",
                        "inode ids exceed inode usage for %s" % project_id)
                if project["blocks"] < 0 or project["inodes"] < 0:
                    raise QuotaError(
                        "corrupt-snapshot",
                        "negative usage for %s" % project_id)
            self._projects = restored
            self._handles = {}
            self._next_handle = 0
            return self
