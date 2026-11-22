"""NFSv4.1 lease, lock, delegation and layout state keeper (skeleton)."""

NFS4ERR_GRACE = "NFS4ERR_GRACE"
NFS4ERR_ACCESS = "NFS4ERR_ACCESS"
NFS4ERR_BAD_STATEID = "NFS4ERR_BAD_STATEID"
NFS4ERR_CLID_INUSE = "NFS4ERR_CLID_INUSE"
NFS4ERR_STALE = "NFS4ERR_STALE"
NFS4ERR_RESOURCE = "NFS4ERR_RESOURCE"


class NfsLeaseError(Exception):
    def __init__(self, code, message=""):
        self.code = code
        super().__init__(code if not message else "%s: %s" % (code, message))


class NfsLeaseKeeper:
    def __init__(self, lease_seconds=90, grace_seconds=30, exports=(), max_states=10000):
        self.lease_seconds = lease_seconds
        self.grace_seconds = grace_seconds
        self.exports = list(exports)
        self.max_states = max_states
        self._clock = 0.0

    def advance(self, seconds):
        raise NotImplementedError

    def start_grace(self):
        raise NotImplementedError

    def end_grace(self):
        raise NotImplementedError

    def in_grace(self):
        raise NotImplementedError

    def normalize_export(self, path):
        raise NotImplementedError

    def authorize_export(self, path):
        raise NotImplementedError

    def setclientid(self, client_id, owner):
        raise NotImplementedError

    def confirm(self, client_id, token):
        raise NotImplementedError

    def open_file(self, client_id, file_handle, *, reclaim=False):
        raise NotImplementedError

    def renew(self, client_id, state_id, *, queue_delay=0, now=None):
        raise NotImplementedError

    def delegate(self, client_id, state_id):
        raise NotImplementedError

    def delegreturn(self, client_id, state_id):
        raise NotImplementedError

    def acquire_lock(self, client_id, state_id, lock_key):
        raise NotImplementedError

    def release_lock(self, client_id, state_id, lock_key):
        raise NotImplementedError

    def layoutget(self, client_id, state_id):
        raise NotImplementedError

    def expire(self, now=None):
        raise NotImplementedError

    def callback_timeout(self, client_id):
        raise NotImplementedError

    def disconnect_client(self, client_id):
        raise NotImplementedError

    def close_export(self, path):
        raise NotImplementedError

    def state_count(self, client_id=None):
        raise NotImplementedError

    def lock_count(self, client_id=None):
        raise NotImplementedError

    def delegation_count(self, client_id=None):
        raise NotImplementedError

    def snapshot(self):
        raise NotImplementedError

    def restore(self, snapshot):
        raise NotImplementedError
