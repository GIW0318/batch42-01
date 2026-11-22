"""SRTP key epoch and per-SSRC replay state keeper (skeleton)."""


class SrtpError(Exception):
    def __init__(self, code, message=""):
        self.code = code
        super().__init__(code if not message else "%s: %s" % (code, message))


class SrtpEpochKeeper:
    def __init__(self):
        pass

    def add_stream(self, ssrc, *, addr=None):
        raise NotImplementedError

    def install_key(self, ssrc, epoch, key, *, active=False, expires_at=None):
        raise NotImplementedError

    def activate(self, ssrc, epoch):
        raise NotImplementedError

    def confirm(self, ssrc, epoch):
        raise NotImplementedError

    def active_epoch(self, ssrc):
        raise NotImplementedError

    def epochs(self, ssrc):
        raise NotImplementedError

    def protect(self, ssrc, seq, payload, *, epoch=None, control=False,
                retransmit=False, now=0.0):
        raise NotImplementedError

    def unprotect(self, ssrc, packet, *, src_addr=None):
        raise NotImplementedError

    def replay_count(self, ssrc):
        raise NotImplementedError

    def next_seq(self, ssrc, *, control=False):
        raise NotImplementedError

    def bind_address(self, ssrc, addr):
        raise NotImplementedError

    def switch_ssrc(self, ssrc):
        raise NotImplementedError

    def expire_keys(self, now):
        raise NotImplementedError

    def retire(self, ssrc, epoch):
        raise NotImplementedError

    def snapshot(self):
        raise NotImplementedError

    def restore(self, snapshot):
        raise NotImplementedError

    def audit_events(self):
        raise NotImplementedError
