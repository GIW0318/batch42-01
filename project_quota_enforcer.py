"""Project quota reservation and accounting skeleton."""

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

    def create_project(self, project_id, parent=None, block_limit=None,
                       inode_limit=None):
        raise NotImplementedError

    def set_quota(self, project_id, block_limit=None, inode_limit=None):
        raise NotImplementedError

    def usage(self, project_id):
        raise NotImplementedError

    def reserve(self, project_id, blocks=0, inodes=0, inode_id=None,
                kind="file", sparse=False, apparent_blocks=None, path=""):
        raise NotImplementedError

    def release(self, project_id, blocks=0, inodes=0, inode_id=None):
        raise NotImplementedError

    def open_file(self, project_id, path):
        raise NotImplementedError

    def write(self, handle, blocks=0, inodes=0):
        raise NotImplementedError

    def rename(self, src_project, dst_project, path, blocks=0):
        raise NotImplementedError

    def truncate(self, project_id, path, new_blocks):
        raise NotImplementedError

    def advance(self, seconds):
        raise NotImplementedError

    def snapshot(self):
        raise NotImplementedError

    def restore(self, snapshot):
        raise NotImplementedError
