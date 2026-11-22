"""Kerberos ticket realm/principal/kvno/replay/PAC validation (skeleton)."""

import copy
import threading
import time


class KerberosError(Exception):
    def __init__(self, code, message=""):
        self.code = code
        super().__init__(code if not message else "%s: %s" % (code, message))


def _split_principal(name):
    if not isinstance(name, str) or "@" not in name:
        return (), ""
    component, _, realm = name.rpartition("@")
    return tuple(component.split("/")), realm


def _principal_key(name):
    components, realm = _split_principal(name)
    return (components, realm)


def _entry_expired(entry, now):
    return entry.get("expires_at") is not None and now > entry["expires_at"]


class KerberosTicketGuard:
    def __init__(self, clock_skew=300, replay_ttl=300):
        self.clock_skew = clock_skew
        self.replay_ttl = replay_ttl
        self._lock = threading.RLock()
        self._realm_kvno = {}
        self._principal_keys = {}
        self._delegation = {}
        self._replay_cache = {}
        self._audit = []

    def add_realm(self, realm, current_kvno=1):
        with self._lock:
            self._realm_kvno[realm] = current_kvno

    def rotate_keytab(self, principal, new_kvno, *, now=0.0, overlap=300.0):
        with self._lock:
            key = _principal_key(principal)
            components, realm = key
            previous = self._principal_keys.get(key)
            retired = []
            if previous is not None:
                retired.append(
                    {"kvno": previous["current"], "overlap_end": now + overlap}
                )
                retired.extend(previous.get("retired", ()))
            else:
                realm_kvno = self._realm_kvno.get(realm)
                if realm_kvno is not None and realm_kvno != new_kvno:
                    retired.append(
                        {"kvno": realm_kvno, "overlap_end": now + overlap}
                    )
            self._principal_keys[key] = {"current": new_kvno, "retired": retired}
            if realm and realm in self._realm_kvno:
                self._realm_kvno[realm] = new_kvno

    def authorize_delegation(self, principal, target_service):
        with self._lock:
            self._delegation.setdefault(principal, set()).add(target_service)

    def _deny(self, code, ticket, now, detail):
        self._audit.append(
            {
                "result": "denied",
                "error": code,
                "detail": detail,
                "realm": ticket.get("realm"),
                "principal": ticket.get("principal"),
                "kvno": ticket.get("kvno"),
                "ticket_id": ticket.get("ticket_id"),
                "time": now,
            }
        )
        raise KerberosError(code, detail)

    def _kvno_allowed(self, ticket, now):
        kvno = ticket.get("kvno")
        state = self._principal_keys.get(_principal_key(ticket.get("service")))
        if state is not None:
            if kvno == state["current"]:
                return True
            for retired in state.get("retired", ()):
                if kvno == retired["kvno"] and now <= retired["overlap_end"]:
                    return True
            return False
        current = self._realm_kvno.get(ticket.get("realm"))
        return current is not None and kvno == current

    def _validate(self, ticket, client_addr, now, service):
        realm = ticket.get("realm")
        if realm not in self._realm_kvno:
            self._deny("REALM_MISMATCH", ticket, now, "untrusted realm %r" % realm)

        requested = service if service is not None else ticket.get("service")
        if _principal_key(requested) != _principal_key(ticket.get("principal")):
            self._deny(
                "PRINCIPAL_MISMATCH",
                ticket,
                now,
                "principal %r does not match %r" % (ticket.get("principal"), requested),
            )

        if not self._kvno_allowed(ticket, now):
            self._deny(
                "KVNO_MISMATCH",
                ticket,
                now,
                "kvno %r is not current" % ticket.get("kvno"),
            )

        if not ticket.get("pac_signature_valid"):
            self._deny("PAC_INVALID", ticket, now, "PAC signature verification failed")

        client_addrs = ticket.get("client_addrs")
        if not client_addrs or client_addr not in client_addrs:
            self._deny(
                "ADDR_RESTRICTED",
                ticket,
                now,
                "client address %r is not bound to the ticket" % client_addr,
            )

        issue_time = ticket.get("issue_time", 0.0)
        expiry = ticket.get("expiry", 0.0)
        if now < issue_time - self.clock_skew or now > expiry + self.clock_skew:
            self._deny(
                "CLOCK_SKEW",
                ticket,
                now,
                "ticket time window [%s, %s] outside clock skew %s"
                % (issue_time, expiry, self.clock_skew),
            )

        for referred in ticket.get("referral_chain", ()) or ():
            if referred not in self._realm_kvno:
                self._deny(
                    "UNTRUSTED_REFERRAL",
                    ticket,
                    now,
                    "referral realm %r is not in the trust chain" % referred,
                )

    def authenticate(self, ticket, client_addr, *, now=0.0, service=None):
        with self._lock:
            self._validate(ticket, client_addr, now, service)

            replay_key = (ticket.get("ticket_id"), client_addr)
            previous = self._replay_cache.get(replay_key)
            if previous is not None and not _entry_expired(previous, now):
                self._deny(
                    "REPLAY",
                    ticket,
                    now,
                    "ticket %r replayed from %r"
                    % (ticket.get("ticket_id"), client_addr),
                )

            self._replay_cache[replay_key] = {
                "ticket_id": ticket.get("ticket_id"),
                "client_addr": client_addr,
                "micro_ts": time.time_ns() // 1000,
                "seen_at": now,
                "expires_at": now + self.replay_ttl,
            }
            result = {
                "realm": ticket.get("realm"),
                "principal": ticket.get("principal"),
                "kvno": ticket.get("kvno"),
                "ticket_id": ticket.get("ticket_id"),
                "client_addr": client_addr,
            }
            self._audit.append(
                {
                    "result": "success",
                    "realm": ticket.get("realm"),
                    "principal": ticket.get("principal"),
                    "kvno": ticket.get("kvno"),
                    "ticket_id": ticket.get("ticket_id"),
                    "client_addr": client_addr,
                    "time": now,
                }
            )
            return result

    def delegate(self, ticket, client_addr, target_service, *, now=0.0):
        with self._lock:
            self._validate(ticket, client_addr, now, None)
            allowed = self._delegation.get(ticket.get("principal"), set())
            if target_service not in allowed:
                self._deny(
                    "DELEGATION_DENIED",
                    ticket,
                    now,
                    "delegation to %r is not authorized" % target_service,
                )

            replay_key = (ticket.get("ticket_id"), client_addr, target_service)
            previous = self._replay_cache.get(replay_key)
            if previous is not None and not _entry_expired(previous, now):
                self._deny(
                    "REPLAY",
                    ticket,
                    now,
                    "delegation ticket %r replayed" % ticket.get("ticket_id"),
                )
            self._replay_cache[replay_key] = {
                "ticket_id": ticket.get("ticket_id"),
                "client_addr": client_addr,
                "target_service": target_service,
                "micro_ts": time.time_ns() // 1000,
                "seen_at": now,
                "expires_at": now + self.replay_ttl,
            }
            self._audit.append(
                {
                    "result": "delegated",
                    "realm": ticket.get("realm"),
                    "principal": ticket.get("principal"),
                    "kvno": ticket.get("kvno"),
                    "ticket_id": ticket.get("ticket_id"),
                    "client_addr": client_addr,
                    "target_service": target_service,
                    "time": now,
                }
            )
            return {
                "realm": ticket.get("realm"),
                "principal": ticket.get("principal"),
                "target_service": target_service,
                "ticket_id": ticket.get("ticket_id"),
            }

    def snapshot(self):
        with self._lock:
            return {
                "clock_skew": self.clock_skew,
                "replay_ttl": self.replay_ttl,
                "realm_kvno": copy.deepcopy(self._realm_kvno),
                "principal_keys": copy.deepcopy(self._principal_keys),
                "delegation": {
                    principal: copy.deepcopy(targets)
                    for principal, targets in self._delegation.items()
                },
                "replay_cache": copy.deepcopy(self._replay_cache),
                "audit": copy.deepcopy(self._audit),
            }

    def restore(self, snapshot):
        with self._lock:
            self.clock_skew = snapshot["clock_skew"]
            self.replay_ttl = snapshot["replay_ttl"]
            self._realm_kvno = copy.deepcopy(snapshot["realm_kvno"])
            self._principal_keys = copy.deepcopy(snapshot["principal_keys"])
            self._delegation = {
                principal: set(targets)
                for principal, targets in snapshot["delegation"].items()
            }
            self._replay_cache = copy.deepcopy(snapshot["replay_cache"])
            self._audit = copy.deepcopy(snapshot["audit"])

    def audit_events(self):
        with self._lock:
            return copy.deepcopy(self._audit)
