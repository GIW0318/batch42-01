"""Transactional container mount/propagation guard (skeleton)."""


class MountPolicyError(Exception):
    def __init__(self, code, message=""):
        self.code = code
        super().__init__(code if not message else "%s: %s" % (code, message))


class MountPropagationGuard:
    def __init__(self, max_mounts=1024, allowed_roots=("/srv/volumes",),
                 uid_map=None):
        self.max_mounts = max_mounts
        self.allowed_roots = list(allowed_roots)
        self.uid_map = dict(uid_map or {})

    def create_container(self, container_id, mounts):
        raise NotImplementedError

    def mount(self, container_id, source, target, *, readonly=False,
              propagation="private", owner_uid=None, realpath=None):
        raise NotImplementedError

    def remount(self, container_id, target, *, readonly=False):
        raise NotImplementedError

    def umount(self, container_id, target, *, busy=False):
        raise NotImplementedError

    def remove_container(self, container_id):
        raise NotImplementedError

    def mounts(self, container_id):
        raise NotImplementedError

    def host_visible(self):
        raise NotImplementedError

    def mount_count(self):
        raise NotImplementedError

    def snapshot(self):
        raise NotImplementedError

    def restore(self, snapshot):
        raise NotImplementedError
