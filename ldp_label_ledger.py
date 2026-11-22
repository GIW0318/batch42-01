"""LDP FEC/label ledger skeleton."""

AUTH_FAILED = "auth-failed"
STALE_SEQUENCE = "stale-sequence"
INVALID_LABEL = "invalid-label"
RESOURCE_EXHAUSTED = "resource-exhausted"
TTL_EXCEEDED = "ttl-exceeded"
UNKNOWN_LABEL = "unknown-label"


class LdpError(Exception):
    def __init__(self, code, message=""):
        self.code = code
        super().__init__(code if not message else "%s: %s" % (code, message))


class LdpLabelLedger:
    """FEC to label bindings, label pool and per-peer session state.

    authenticate() receives a hex HMAC-SHA256 digest over the message payload
    using the configured session secret.  Released labels stay quarantined
    for quarantine_seconds before they can be handed out again.
    """

    def __init__(self, label_min=16, label_max=1000, secret=None,
                 quarantine_seconds=30):
        self.label_min = label_min
        self.label_max = label_max
        self.secret = secret
        self.quarantine_seconds = quarantine_seconds
        self._clock = 0

    def open_session(self, peer, transport, password=None):
        raise NotImplementedError

    def authenticate(self, peer, message_id, payload, digest):
        raise NotImplementedError

    def advertise(self, peer, fec, label, seq, local_pref=100, metric=10):
        raise NotImplementedError

    def withdraw(self, peer, fec, seq):
        raise NotImplementedError

    def route(self, fec):
        raise NotImplementedError

    def label_in_use(self, label):
        raise NotImplementedError

    def request_label(self, peer, fec):
        raise NotImplementedError

    def release_label(self, peer, label, seq):
        raise NotImplementedError

    def session_down(self, peer):
        raise NotImplementedError

    def set_transport(self, peer, transport):
        raise NotImplementedError

    def transport_of(self, peer):
        raise NotImplementedError

    def check_ttl(self, ttl):
        raise NotImplementedError

    def handle_notification(self, peer, status_code):
        raise NotImplementedError

    def reconcile(self, peer, retained_fecs):
        raise NotImplementedError

    def advance(self, seconds):
        raise NotImplementedError

    def snapshot(self):
        raise NotImplementedError

    def restore(self, snapshot):
        raise NotImplementedError
