"""Per-recipient-domain SMTP STARTTLS policy and delivery gate."""

import copy
import threading


TLSA_FAILURES = {"timeout", "error", "bogus", "invalid", "failed"}

DEFAULT_MODE = "optional"


class SmtpPolicyError(Exception):
    def __init__(self, code, message=""):
        self.code = code
        super().__init__(code if not message else "%s: %s" % (code, message))


class StartTlsPolicy:
    def __init__(self, max_retries=5):
        self.max_retries = max_retries
        self._lock = threading.RLock()
        self._policies = {}
        self._versions = {}
        self._loader_locks = {}
        self._connections = {}
        self._next_connection_id = 0
        self._queue = {}
        self._audit = []

    def set_domain_policy(self, domain, mode):
        if mode not in ("require", "optional"):
            raise SmtpPolicyError("INVALID_POLICY_MODE", mode)
        with self._lock:
            version = self._versions.get(domain, 0) + 1
            self._versions[domain] = version
            self._policies[domain] = {"mode": mode, "version": version}
            return version

    def get_policy(self, domain, loader=None):
        with self._lock:
            policy = self._policies.get(domain)
            if policy is not None:
                return dict(policy)
        if loader is None:
            return {"mode": DEFAULT_MODE, "version": 0}
        loader_lock = self._loader_locks.setdefault(domain, threading.Lock())
        with loader_lock:
            with self._lock:
                policy = self._policies.get(domain)
                if policy is not None:
                    return dict(policy)
            mode = loader(domain)
            with self._lock:
                policy = self._policies.get(domain)
                if policy is None:
                    version = self._versions.get(domain, 0) + 1
                    self._versions[domain] = version
                    policy = {"mode": mode, "version": version}
                    self._policies[domain] = policy
                return dict(policy)

    def expire_policy(self, domain):
        with self._lock:
            self._policies.pop(domain, None)

    def open_connection(self, domain):
        policy = self.get_policy(domain)
        with self._lock:
            self._next_connection_id += 1
            connection_id = self._next_connection_id
            self._connections[connection_id] = {
                "id": connection_id,
                "domain": domain,
                "policy_version": policy["version"],
            }
            return dict(self._connections[connection_id])

    def send_message(self, connection_id, queue_id):
        with self._lock:
            connection = self._connections.get(connection_id)
            if connection is None:
                raise SmtpPolicyError("UNKNOWN_CONNECTION", str(connection_id))
            current = self._versions.get(connection["domain"], 0)
            if connection["policy_version"] != current:
                raise SmtpPolicyError(
                    "RECONNECT_REQUIRED",
                    "connection bound to policy v%s, current is v%s"
                    % (connection["policy_version"], current),
                )
        return True

    def queue_message(self, queue_id, domain, payload, policy_version=None):
        policy = self.get_policy(domain)
        if isinstance(payload, str):
            payload = payload.encode("utf-8")
        with self._lock:
            self._queue[queue_id] = {
                "queue_id": queue_id,
                "domain": domain,
                "payload": bytes(payload),
                "policy_mode": policy["mode"],
                "policy_version": (
                    policy["version"] if policy_version is None else policy_version
                ),
                "attempts": 0,
                "status": "queued",
            }
            return dict(self._queue[queue_id])

    def message_status(self, queue_id):
        with self._lock:
            message = self._queue[queue_id]
            return {
                "queue_id": queue_id,
                "domain": message["domain"],
                "status": message["status"],
                "attempts": message["attempts"],
                "policy_version": message["policy_version"],
            }

    def prepare_payload(self, queue_id, *, downgrade=False):
        with self._lock:
            return self._queue[queue_id]["payload"]

    def retry(self, queue_id):
        with self._lock:
            message = self._queue[queue_id]
            if message["attempts"] >= self.max_retries:
                message["status"] = "bounced"
                return {"status": "bounced", "attempts": message["attempts"]}
            message["attempts"] += 1
            message["status"] = "retry"
            return {"status": "retry", "attempts": message["attempts"]}

    def _evaluate(self, domain, policy, *, starttls, tlsa_status, cert_hostname,
                  mx_host, remote_code):
        mode = policy["mode"]
        if mode == "require" and not starttls:
            return {"action": "defer", "reason": "TLS_REQUIRED", "plaintext": False}
        if starttls and tlsa_status in TLSA_FAILURES:
            return {"action": "defer", "reason": "TLSA_FAILED", "plaintext": False}
        if (starttls and cert_hostname is not None and mx_host is not None
                and cert_hostname != mx_host):
            return {
                "action": "defer",
                "reason": "CERT_HOSTNAME_MISMATCH",
                "plaintext": False,
            }
        if remote_code is not None:
            if 400 <= remote_code < 500:
                return {"action": "retry", "reason": "TEMP_FAILURE",
                        "plaintext": not starttls}
            if 500 <= remote_code < 600:
                return {"action": "bounce", "reason": "PERM_FAILURE",
                        "plaintext": not starttls}
        return {"action": "deliver", "reason": "OK", "plaintext": not starttls}

    def _record_audit(self, domain, policy, decision, queue_id):
        with self._lock:
            self._audit.append({
                "queue_id": queue_id,
                "domain": domain,
                "action": decision["action"],
                "reason": decision["reason"],
                "policy_version": policy["version"],
            })

    def decide(self, domain, *, starttls, tlsa_status="valid", cert_hostname=None,
               mx_host=None, remote_code=None, queue_id=None):
        policy = self.get_policy(domain)
        decision = self._evaluate(
            domain, policy, starttls=starttls, tlsa_status=tlsa_status,
            cert_hostname=cert_hostname, mx_host=mx_host, remote_code=remote_code,
        )
        self._record_audit(domain, policy, decision, queue_id)
        return decision

    def plan_delivery(self, domain, mx_records):
        policy = self.get_policy(domain)
        capable = [
            mx for mx in mx_records
            if mx.get("starttls") and mx.get("tlsa", "none") not in TLSA_FAILURES
        ]
        if capable:
            return {"action": "deliver", "host": capable[0]["host"],
                    "plaintext": False}
        if policy["mode"] == "require":
            return {"action": "defer", "reason": "NO_TLS_CAPABLE_MX",
                    "plaintext": False}
        return {"action": "deliver", "host": mx_records[0]["host"],
                "plaintext": True}

    def decide_for_queued(self, queue_id, *, starttls=False, tlsa_status="valid",
                          cert_hostname=None, mx_host=None, remote_code=None):
        with self._lock:
            message = self._queue[queue_id]
            policy = {
                "mode": message["policy_mode"],
                "version": message["policy_version"],
            }
            domain = message["domain"]
        decision = self._evaluate(
            domain, policy, starttls=starttls, tlsa_status=tlsa_status,
            cert_hostname=cert_hostname, mx_host=mx_host, remote_code=remote_code,
        )
        self._record_audit(domain, policy, decision, queue_id)
        return decision

    def snapshot(self):
        with self._lock:
            return {
                "policies": copy.deepcopy(self._policies),
                "versions": dict(self._versions),
                "queue": copy.deepcopy(self._queue),
                "max_retries": self.max_retries,
            }

    def restore(self, snapshot):
        with self._lock:
            self._policies = copy.deepcopy(snapshot.get("policies", {}))
            self._versions = dict(snapshot.get("versions", {}))
            self._queue = copy.deepcopy(snapshot.get("queue", {}))
            self.max_retries = snapshot.get("max_retries", self.max_retries)

    def audit_events(self):
        with self._lock:
            return [dict(event) for event in self._audit]
