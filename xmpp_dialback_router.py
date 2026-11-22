"""XMPP dialback verification and stanza routing."""

import hashlib
import hmac
import json

NOT_AUTHORIZED = "not-authorized"
STANZA_TOO_LARGE = "stanza-too-large"
HOP_LIMIT = "hop-limit"
QUEUE_FULL = "queue-full"
UNKNOWN_SESSION = "unknown-session"

_JID_ESCAPES = {
    "20": " ",
    "22": '"',
    "26": "&",
    "27": "'",
    "2f": "/",
    "3a": ":",
    "3c": "<",
    "3e": ">",
    "40": "@",
    "5c": "\\",
}


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
        self._resources = {}
        self._offline = {}
        self._verified = set()
        self._dialback_cache = {}
        self._seen = set()
        self._audit = []

    @staticmethod
    def _unescape(localpart):
        out = []
        idx = 0
        while idx < len(localpart):
            if (localpart[idx] == "\\" and idx + 2 < len(localpart)
                    and localpart[idx + 1:idx + 3].lower() in _JID_ESCAPES):
                out.append(_JID_ESCAPES[localpart[idx + 1:idx + 3].lower()])
                idx += 3
            else:
                out.append(localpart[idx])
                idx += 1
        return "".join(out)

    def normalize_jid(self, jid):
        if not jid:
            raise XmppError(NOT_AUTHORIZED, "empty jid")
        if "/" in jid:
            bare, resource = jid.split("/", 1)
        else:
            bare, resource = jid, None
        if "@" in bare:
            localpart, domain = bare.rsplit("@", 1)
            localpart = self._unescape(localpart).lower()
            bare = "%s@%s" % (localpart, domain.lower())
        else:
            bare = bare.lower()
        if resource:
            return "%s/%s" % (bare, resource)
        return bare

    @staticmethod
    def _bare(jid):
        return jid.split("/", 1)[0]

    @staticmethod
    def _domain(jid):
        return jid.split("/", 1)[0].rsplit("@", 1)[-1]

    def dialback_key(self, from_domain, to_domain, stream_id, secret):
        message = "%s|%s|%s" % (from_domain, to_domain, stream_id)
        return hmac.new(secret, message.encode("utf-8"),
                        hashlib.sha256).hexdigest()

    def verify_dialback(self, from_domain, to_domain, stream_id, key, secret,
                        version=1):
        cache_key = (from_domain, to_domain, stream_id, version)
        if cache_key in self._dialback_cache:
            return self._dialback_cache[cache_key]
        expected = self.dialback_key(from_domain, to_domain, stream_id, secret)
        ok = hmac.compare_digest(expected, key)
        self._dialback_cache[cache_key] = ok
        if ok:
            self._verified.add((from_domain, to_domain, version))
        self._audit.append({
            "kind": "dialback",
            "from": from_domain,
            "to": to_domain,
            "version": version,
            "decision": "verified" if ok else "rejected",
        })
        return ok

    def _is_verified(self, remote_domain):
        return any(pair_from == remote_domain and pair_to == self.local_domain
                   for pair_from, pair_to, _ in self._verified)

    def bind_resource(self, jid, priority=0):
        full = self.normalize_jid(jid)
        bare = self._bare(full)
        self._resources.setdefault(bare, {})[full] = priority
        return full

    def route(self, stanza, hops=0):
        if hops > self.max_hops:
            raise XmppError(HOP_LIMIT, "too many hops")
        size = len(json.dumps(stanza, sort_keys=True).encode("utf-8"))
        if size > self.max_stanza_bytes:
            raise XmppError(STANZA_TOO_LARGE, "stanza exceeds %d bytes"
                            % self.max_stanza_bytes)
        from_jid = self.normalize_jid(stanza.get("from", ""))
        to_jid = self.normalize_jid(stanza.get("to", ""))
        from_domain = self._domain(from_jid)
        to_domain = self._domain(to_jid)
        for domain in (from_domain, to_domain):
            if domain != self.local_domain and not self._is_verified(domain):
                raise XmppError(NOT_AUTHORIZED,
                                "unverified domain %s" % domain)
        dedup_key = (stanza.get("stream"), from_jid, to_jid, stanza.get("id"))
        if dedup_key in self._seen:
            self._audit.append({
                "kind": "route",
                "from": from_jid,
                "to": to_jid,
                "decision": "duplicate",
            })
            return {"deliveries": [], "stored": False, "duplicate": True}
        self._seen.add(dedup_key)

        deliveries = []
        stored = False
        if to_domain == self.local_domain:
            bound = self._resources.get(self._bare(to_jid), {})
            if "/" in to_jid and to_jid in bound:
                deliveries = [to_jid]
            elif bound:
                top = max(bound.values())
                deliveries = sorted(jid for jid, pri in bound.items()
                                    if pri == top)
            else:
                self.offline_enqueue(self._bare(to_jid), stanza)
                stored = True
            decision = "stored" if stored else "delivered"
        else:
            deliveries = [to_jid]
            decision = "forwarded"
        self._audit.append({
            "kind": "route",
            "from": from_jid,
            "to": to_jid,
            "decision": decision,
        })
        return {"deliveries": deliveries, "stored": stored, "duplicate": False}

    def sasl_external(self, cert):
        addrs = cert.get("xmppAddr") if isinstance(cert, dict) else None
        if not addrs:
            raise XmppError(NOT_AUTHORIZED, "certificate has no xmppAddr")
        jid = self.normalize_jid(addrs[0])
        if self._domain(jid) != self.local_domain:
            raise XmppError(NOT_AUTHORIZED,
                            "xmppAddr domain is not %s" % self.local_domain)
        return jid

    def offline_enqueue(self, jid, stanza):
        bare = self._bare(self.normalize_jid(jid))
        queue = self._offline.setdefault(bare, [])
        if len(queue) >= self.max_offline:
            raise XmppError(QUEUE_FULL, "offline queue full for %s" % bare)
        queue.append(dict(stanza))
        return True

    def drain_offline(self, jid):
        bare = self._bare(self.normalize_jid(jid))
        return self._offline.pop(bare, [])

    def restart_stream(self, stream_id):
        self._seen = {key for key in self._seen if key[0] != stream_id}
        self._dialback_cache = {
            key: value for key, value in self._dialback_cache.items()
            if key[2] != stream_id
        }
        return True

    def audit_log(self):
        return list(self._audit)

    def snapshot(self):
        return {
            "local_domain": self.local_domain,
            "offline": {bare: [dict(stanza) for stanza in queue]
                        for bare, queue in self._offline.items()},
            "resources": {bare: dict(bound)
                          for bare, bound in self._resources.items()},
            "verified": sorted(self._verified),
        }

    def restore(self, snapshot):
        self._offline = {bare: [dict(stanza) for stanza in queue]
                         for bare, queue in snapshot.get("offline", {}).items()}
        self._resources = {bare: dict(bound) for bare, bound
                           in snapshot.get("resources", {}).items()}
        self._verified = {tuple(item)
                          for item in snapshot.get("verified", [])}
        return True
