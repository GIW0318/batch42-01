"""SCTP multi-stream, PR-SCTP, path failover and AUTH guard (skeleton)."""

AUTH_FAILURE_CAUSE = "AUTH_FAILURE"


class SctpError(Exception):
    def __init__(self, code, cause=None, message=""):
        self.code = code
        self.cause = cause
        super().__init__(code if not message else "%s: %s" % (code, message))


class SctpStreamGuard:
    def __init__(self, peer_streams=32):
        self.peer_streams = peer_streams

    def register_association(self, assoc_id, peer_streams=None):
        raise NotImplementedError

    def send(self, assoc_id, stream_id, payload):
        raise NotImplementedError

    def abandon(self, tsn):
        raise NotImplementedError

    def pending(self, assoc_id):
        raise NotImplementedError

    def reassembly_bytes(self, assoc_id, stream_id=None):
        raise NotImplementedError

    def receive_chunk(self, assoc_id, stream_id, tsn, ssn, data, *, last=False):
        raise NotImplementedError

    def duplicate_count(self, assoc_id, stream_id=None):
        raise NotImplementedError

    def sweep_reassembly(self, now=None):
        raise NotImplementedError

    def set_rwnd(self, assoc_id, stream_id, rwnd):
        raise NotImplementedError

    def add_path(self, assoc_id, path_id, primary=False):
        raise NotImplementedError

    def set_path_state(self, assoc_id, path_id, reachable):
        raise NotImplementedError

    def heartbeat_timeout(self, assoc_id, path_id):
        raise NotImplementedError

    def path_state(self, assoc_id, path_id):
        raise NotImplementedError

    def active_path(self, assoc_id):
        raise NotImplementedError

    def cumack(self, assoc_id, tsn, path_id):
        raise NotImplementedError

    def unacked_tsns(self, assoc_id, path_id=None):
        raise NotImplementedError

    def authenticate(self, assoc_id, chunk, *, valid=True):
        raise NotImplementedError

    def association_closed(self, assoc_id):
        raise NotImplementedError

    def add_route(self, prefix, source):
        raise NotImplementedError

    def select_source(self, assoc_id, dest):
        raise NotImplementedError

    def next_ssn(self, assoc_id, stream_id):
        raise NotImplementedError

    def snapshot(self, assoc_id):
        raise NotImplementedError

    def restore(self, assoc_id, snapshot):
        raise NotImplementedError
