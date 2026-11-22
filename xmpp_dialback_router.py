"""XMPP dialback verification and stanza routing skeleton."""

NOT_AUTHORIZED = "not-authorized"
STANZA_TOO_LARGE = "stanza-too-large"
HOP_LIMIT = "hop-limit"
QUEUE_FULL = "queue-full"
UNKNOWN_SESSION = "unknown-session"


class XmppError(Exception):
    def __init__(self, code, message=""):
        self.code = code
        super().__init__(code if not message else "%s: %s" % (code, message))


class XmppDialbackRouter:
    """JID normalization, dialback keys, resource routing and offline queue.

    route() returns {"deliveries": [...], "stored": bool, "duplicate": bool}.
    Dialback keys are hex HMAC-SHA256 values bound to the (from, to, stream)
    triple; verified domains are cached per domain and key version.
    """

    def __init__(self, local_domain="example.com", max_stanza_bytes=65536,
                 max_hops=10, max_offline=1000):
        self.local_domain = local_domain
        self.max_stanza_bytes = max_stanza_bytes
        self.max_hops = max_hops
        self.max_offline = max_offline

    def normalize_jid(self, jid):
        raise NotImplementedError

    def dialback_key(self, from_domain, to_domain, stream_id, secret):
        raise NotImplementedError

    def verify_dialback(self, from_domain, to_domain, stream_id, key, secret,
                        version=1):
        raise NotImplementedError

    def bind_resource(self, jid, priority=0):
        raise NotImplementedError

    def route(self, stanza, hops=0):
        raise NotImplementedError

    def sasl_external(self, cert):
        raise NotImplementedError

    def offline_enqueue(self, jid, stanza):
        raise NotImplementedError

    def drain_offline(self, jid):
        raise NotImplementedError

    def restart_stream(self, stream_id):
        raise NotImplementedError

    def audit_log(self):
        raise NotImplementedError

    def snapshot(self):
        raise NotImplementedError

    def restore(self, snapshot):
        raise NotImplementedError
