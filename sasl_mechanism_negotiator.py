"""SASL mechanism negotiation and multi-round state management.

The negotiator selects the strongest mechanism supported by both peers,
guards the EXTERNAL mechanism behind a verifiable client certificate, keeps
multi-round challenge state isolated per connection under a lock, enforces
the distinction between the authentication and authorization identities, and
records every attempted downgrade in an audit log.
"""

import threading

UNKNOWN_MECHANISM = "unknown-mechanism"
NO_MECHANISM = "no-mechanism"
CERTIFICATE_REQUIRED = "certificate-required"
INVALID_AUTHZID = "invalid-authzid"
INVALID_AUTHCID = "invalid-authcid"
AUTHZID_NOT_ALLOWED = "authzid-not-allowed"
RESPONSE_TOO_LARGE = "response-too-large"
CHANNEL_BINDING_REQUIRED = "channel-binding-required"
NO_SESSION = "no-session"
SESSION_BUSY = "session-busy"
ACCOUNT_LOCKED = "account-locked"


class SaslError(Exception):
    def __init__(self, code, message=""):
        self.code = code
        super().__init__(code if not message else "%s: %s" % (code, message))


class _Session:
    """Per-connection authentication state."""

    def __init__(self, conn_id, mechanism, authcid):
        self.conn_id = conn_id
        self.mechanism = mechanism
        self.authcid = authcid
        self.authzid = None
        self.state = "in-progress"
        self.security_layer = "none"
        self.writable = True
        self.bound = False
        self.duplicate = False
        self.round = 0


class SaslMechanismNegotiator:
    """Chooses the strongest mutually supported SASL mechanism.

    Mechanism names are case-insensitive.  EXTERNAL needs a client
    certificate and an explicit authorization identity.  Multi-round state is
    per connection and abort clears it.  Failure counters are per account.
    """

    STRENGTH = {
        "EXTERNAL": 5,
        "GSSAPI": 4,
        "SCRAM-SHA-256": 3,
        "PLAIN": 1,
    }

    def __init__(self, mechanisms=("EXTERNAL", "GSSAPI", "SCRAM-SHA-256", "PLAIN"),
                 max_initial=1024, require_channel_binding=False,
                 proxy_allowed=(), max_failures=3):
        self.mechanisms = tuple(name.upper() for name in mechanisms)
        self.max_initial = max_initial
        self.require_channel_binding = require_channel_binding
        self.proxy_allowed = tuple(proxy_allowed)
        self.max_failures = max_failures
        self._sessions = {}
        self._conn_locks = {}
        self._registry_lock = threading.Lock()
        self._failures = {}
        self._failures_lock = threading.Lock()
        self._audit = []

    def _audit_record(self, kind, conn_id, code, detail=""):
        entry = {"kind": kind, "conn_id": conn_id, "code": code}
        if detail:
            entry["detail"] = detail
        self._audit.append(entry)

    def _lock_for(self, conn_id):
        with self._registry_lock:
            lock = self._conn_locks.get(conn_id)
            if lock is None:
                lock = threading.RLock()
                self._conn_locks[conn_id] = lock
            return lock

    def _session(self, conn_id):
        return self._sessions.get(conn_id)

    def _require_live_session(self, conn_id):
        session = self._session(conn_id)
        if session is None or session.state == "aborted":
            raise SaslError(NO_SESSION, "conn_id=%s" % conn_id)
        return session

    def negotiate(self, conn_id, client_mechanisms, has_client_cert=False,
                  channel_binding=False):
        with self._lock_for(conn_id):
            existing = self._session(conn_id)
            if existing is not None and existing.state != "aborted":
                raise SaslError(SESSION_BUSY, "conn_id=%s" % conn_id)

            if self.require_channel_binding and not channel_binding:
                self._audit_record("downgrade", conn_id,
                                   CHANNEL_BINDING_REQUIRED,
                                   "peer offered no channel binding")
                raise SaslError(CHANNEL_BINDING_REQUIRED,
                                "channel binding is mandatory")

            if not client_mechanisms:
                raise SaslError(NO_MECHANISM, "client offered no mechanisms")

            normalized = []
            for name in client_mechanisms:
                canonical = name.upper()
                if canonical not in self.STRENGTH:
                    raise SaslError(UNKNOWN_MECHANISM, name)
                normalized.append(canonical)

            common = [name for name in normalized if name in self.mechanisms]
            if not common:
                raise SaslError(NO_MECHANISM, "no mutually supported mechanism")

            selected = max(common, key=lambda name: self.STRENGTH[name])

            if selected == "EXTERNAL" and not has_client_cert:
                self._audit_record("downgrade", conn_id, CERTIFICATE_REQUIRED,
                                   "EXTERNAL selected without client certificate")
                raise SaslError(CERTIFICATE_REQUIRED,
                                "EXTERNAL requires a verifiable client certificate")

            session = _Session(conn_id, selected, None)
            session.state = "negotiated"
            session.writable = True
            self._sessions[conn_id] = session
            return {
                "mechanism": selected,
                "state": session.state,
                "channel_binding": bool(channel_binding),
            }

    def start(self, conn_id, mechanism, initial_response, authcid=None,
              authzid=None, has_client_cert=False):
        with self._lock_for(conn_id):
            session = self._require_live_session(conn_id)

            if authcid is not None and not self.attempt_allowed(authcid):
                raise SaslError(ACCOUNT_LOCKED, authcid)

            if len(initial_response) > self.max_initial:
                raise SaslError(
                    RESPONSE_TOO_LARGE,
                    "initial response %d exceeds limit %d"
                    % (len(initial_response), self.max_initial))

            canonical = mechanism.upper()
            if canonical not in self.STRENGTH:
                raise SaslError(UNKNOWN_MECHANISM, mechanism)
            if canonical != session.mechanism:
                raise SaslError(UNKNOWN_MECHANISM,
                                "%s was not negotiated" % mechanism)

            if canonical == "EXTERNAL":
                if not has_client_cert:
                    self._audit_record("downgrade", conn_id,
                                       CERTIFICATE_REQUIRED,
                                       "EXTERNAL started without client certificate")
                    raise SaslError(CERTIFICATE_REQUIRED,
                                    "EXTERNAL requires a verifiable client certificate")
                if not authzid:
                    raise SaslError(INVALID_AUTHZID,
                                    "EXTERNAL requires an explicit authzid")

            if not authcid:
                raise SaslError(INVALID_AUTHCID,
                                "authentication identity is required")

            session.authcid = authcid
            session.authzid = authzid if authzid else authcid
            session.state = "in-progress"
            session.round = 1
            return {
                "mechanism": canonical,
                "state": session.state,
                "authcid": session.authcid,
                "authzid": session.authzid,
                "round": session.round,
            }

    def step(self, conn_id, challenge_response):
        with self._lock_for(conn_id):
            session = self._require_live_session(conn_id)

            if session.state == "authenticated":
                raise SaslError(NO_SESSION, "authentication already completed")
            if session.state != "in-progress":
                raise SaslError(NO_SESSION,
                                "no in-progress authentication on conn_id=%s" % conn_id)

            if len(challenge_response) > self.max_initial:
                raise SaslError(
                    RESPONSE_TOO_LARGE,
                    "challenge response %d exceeds limit %d"
                    % (len(challenge_response), self.max_initial))

            session.round += 1
            if session.mechanism == "GSSAPI":
                session.security_layer = "none"

            session.state = "authenticated"
            session.writable = False
            self._clear_failures(session.authcid)
            return {
                "state": session.state,
                "mechanism": session.mechanism,
                "security_layer": session.security_layer,
                "round": session.round,
            }

    def abort(self, conn_id):
        with self._lock_for(conn_id):
            session = self._session(conn_id)
            if session is None:
                session = _Session(conn_id, None, None)
                self._sessions[conn_id] = session
            session.state = "aborted"
            session.writable = False
            session.round = 0
            return {"state": "aborted", "conn_id": conn_id}

    def bind(self, conn_id, authcid, authzid=None):
        with self._lock_for(conn_id):
            session = self._require_live_session(conn_id)

            if not authcid:
                raise SaslError(INVALID_AUTHCID,
                                "authentication identity is required")
            if session.authcid is not None and session.authcid != authcid:
                raise SaslError(INVALID_AUTHCID,
                                "authcid does not match the authenticated identity")
            session.authcid = authcid

            effective_authzid = authzid if authzid else authcid
            if effective_authzid != authcid and authcid not in self.proxy_allowed:
                self._audit_record("downgrade", conn_id, AUTHZID_NOT_ALLOWED,
                                   "%s may not act as %s"
                                   % (authcid, effective_authzid))
                raise SaslError(AUTHZID_NOT_ALLOWED,
                                "%s is not allowed to assume %s"
                                % (authcid, effective_authzid))

            if session.bound:
                return {
                    "authcid": session.authcid,
                    "authzid": session.authzid,
                    "state": session.state,
                    "duplicate": True,
                }

            session.authzid = effective_authzid
            session.bound = True
            session.state = "authenticated"
            session.writable = False
            self._clear_failures(authcid)
            return {
                "authcid": session.authcid,
                "authzid": session.authzid,
                "state": session.state,
                "duplicate": False,
            }

    def connection_state(self, conn_id):
        session = self._session(conn_id)
        if session is None:
            return "none"
        return session.state

    def security_layer(self, conn_id):
        session = self._session(conn_id)
        if session is None:
            return "none"
        return session.security_layer

    def claims_encrypted(self, conn_id):
        session = self._session(conn_id)
        if session is None:
            return False
        return session.security_layer not in ("none", "", None)

    def record_failure(self, account):
        with self._failures_lock:
            self._failures[account] = self._failures.get(account, 0) + 1
            return self._failures[account]

    def attempt_allowed(self, account):
        with self._failures_lock:
            return self._failures.get(account, 0) < self.max_failures

    def _clear_failures(self, account):
        if account is None:
            return
        with self._failures_lock:
            self._failures.pop(account, None)

    def audit_log(self):
        return list(self._audit)
