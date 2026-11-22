"""SSH port/agent forwarding policy gate (skeleton)."""


class ForwardPolicyError(Exception):
    def __init__(self, code, message=""):
        self.code = code
        super().__init__(code if not message else "%s: %s" % (code, message))


class ForwardPolicyGate:
    def __init__(self, policy=None):
        self.policy = dict(policy or {})

    def set_policy(self, policy):
        raise NotImplementedError

    def open_channel(self, user, kind, dest_host=None, dest_port=None, *,
                     bind_addr=None, socket_path=None, groups=(), resolver=None):
        raise NotImplementedError

    def data(self, channel_id, payload):
        raise NotImplementedError

    def close_channel(self, channel_id):
        raise NotImplementedError

    def reload_policy(self, policy):
        raise NotImplementedError

    def channel_count(self, user):
        raise NotImplementedError

    def open_fd_count(self):
        raise NotImplementedError

    def snapshot_sessions(self):
        raise NotImplementedError

    def restore_sessions(self, snapshot):
        raise NotImplementedError

    def audit_events(self):
        raise NotImplementedError
