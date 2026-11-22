"""Kernel keyring quota, permission and reference accounting broker (skeleton)."""


class KeyringError(Exception):
    def __init__(self, code, message=""):
        self.code = code
        super().__init__(code if not message else "%s: %s" % (code, message))


class KeyringQuotaBroker:
    def __init__(self, max_bytes=65536, max_entries=1024):
        self.max_bytes = max_bytes
        self.max_entries = max_entries

    def add(self, uid, keyring, description, payload, *, perms=0o600,
            timeout=None, now=0.0):
        raise NotImplementedError

    def read(self, uid, keyring, description, *, requester_uid=None, now=0.0):
        raise NotImplementedError

    def update(self, key_id, payload):
        raise NotImplementedError

    def revoke(self, key_id):
        raise NotImplementedError

    def ref(self, key_id):
        raise NotImplementedError

    def unlink(self, key_id):
        raise NotImplementedError

    def process_exit(self, uid, session):
        raise NotImplementedError

    def setuid(self, uid, new_uid):
        raise NotImplementedError

    def link_keyring(self, uid, parent, child):
        raise NotImplementedError

    def search(self, uid, keyring, description):
        raise NotImplementedError

    def quota(self, uid):
        raise NotImplementedError

    def snapshot(self):
        raise NotImplementedError

    def restore(self, snapshot):
        raise NotImplementedError

    def audit_events(self):
        raise NotImplementedError
