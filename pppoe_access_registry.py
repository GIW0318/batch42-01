"""PPPoE access registry keyed by MAC plus session id."""

import threading


class PppoeError(Exception):
    def __init__(self, code, message=""):
        self.code = code
        super().__init__(code if not message else "%s: %s" % (code, message))


class PppoeAccessRegistry:
    def __init__(self, max_sessions_per_account=1, auth_failure_limit=3,
                 idle_timeout=300, discovery_pps=10, session_fee=1.0):
        self.max_sessions_per_account = max_sessions_per_account
        self.auth_failure_limit = auth_failure_limit
        self.idle_timeout = idle_timeout
        self.discovery_pps = discovery_pps
        self.session_fee = session_fee

        self._lock = threading.RLock()
        self._sessions = {}
        self._mac_index = {}
        self._account_index = {}
        self._billing = {}
        self._failures = {}
        self._locked = set()
        self._discovery = {}
        self._retry_queue = []
        self._handles = set()
        self._mtu = {}
        self._epoch = 0
        self._audit = []

    def _audit_event(self, event, **details):
        entry = {"event": event}
        entry.update(details)
        self._audit.append(entry)

    def _find(self, mac, session_id):
        return self._sessions.get((mac, session_id))

    def _release(self, key):
        record = self._sessions.pop(key, None)
        if record is None:
            return None
        self._mac_index.pop(record["mac"], None)
        accounts = self._account_index.get(record["account"])
        if accounts is not None:
            accounts.discard(key)
            if not accounts:
                del self._account_index[record["account"]]
        self._handles.discard(record["handle"])
        self._mtu.pop(record["mac"], None)
        return record

    def dial(self, mac, session_id, account, service="default", now=0.0):
        with self._lock:
            if mac in self._mac_index:
                self._audit_event("dial_rejected", reason="EALREADY",
                                  mac=mac, account=account)
                raise PppoeError("EALREADY", "mac already online: %s" % mac)
            if account in self._locked:
                self._audit_event("dial_rejected", reason="ELOCKED",
                                  mac=mac, account=account)
                raise PppoeError("ELOCKED", "account locked: %s" % account)
            if len(self._account_index.get(account, ())) >= self.max_sessions_per_account:
                self._audit_event("dial_rejected", reason="ELIMIT",
                                  mac=mac, account=account)
                raise PppoeError("ELIMIT",
                                  "session limit reached for account: %s" % account)

            self._epoch += 1
            epoch = self._epoch
            handle = "handle-%d" % epoch
            self._handles.add(handle)
            record = {
                "mac": mac,
                "session_id": session_id,
                "account": account,
                "service": self.normalize_service(service),
                "epoch": epoch,
                "handle": handle,
                "started_at": now,
                "last_activity": now,
                "octets": 0,
                "auth_state": "pending",
            }
            key = (mac, session_id)
            self._sessions[key] = record
            self._mac_index[mac] = key
            self._account_index.setdefault(account, set()).add(key)
            self._billing[account] = self._billing.get(account, 0.0) + self.session_fee
            self._audit_event("dial", mac=mac, session_id=session_id,
                              account=account, epoch=epoch)
            return dict(record)

    def disconnect(self, mac, session_id, epoch):
        with self._lock:
            record = self._find(mac, session_id)
            if record is None or record["epoch"] != epoch or record["mac"] != mac:
                self._audit_event("disconnect_ignored", mac=mac,
                                  session_id=session_id, epoch=epoch)
                return False
            self._release((mac, session_id))
            self._audit_event("disconnect", mac=mac, session_id=session_id,
                              epoch=epoch)
            return True

    def account_sessions(self, account):
        with self._lock:
            return [dict(self._sessions[key])
                    for key in self._account_index.get(account, set())]

    def accounting_total(self, account):
        with self._lock:
            return self._billing.get(account, 0.0)

    def authenticate(self, mac, session_id, *, ok, started_at, now, timeout=30.0):
        with self._lock:
            record = self._find(mac, session_id)
            if record is None:
                raise PppoeError("ENOENT",
                                  "unknown session: %s/%s" % (mac, session_id))
            account = record["account"]
            if account in self._locked:
                self._audit_event("auth_rejected", reason="ELOCKED",
                                  mac=mac, account=account)
                raise PppoeError("ELOCKED", "account locked: %s" % account)
            if now - started_at > timeout:
                self._audit_event("auth_rejected", reason="ETIMEDOUT",
                                  mac=mac, account=account)
                raise PppoeError("ETIMEDOUT", "authentication response too late")
            if not ok:
                failures = self._failures.get(account, 0) + 1
                self._failures[account] = failures
                self._audit_event("auth_failure", mac=mac, account=account,
                                  failures=failures)
                if failures >= self.auth_failure_limit:
                    self._locked.add(account)
                    self._audit_event("account_locked", account=account)
                raise PppoeError("EAUTHFAIL",
                                  "authentication failed for account: %s" % account)
            self._failures[account] = 0
            record["auth_state"] = "authenticated"
            record["last_activity"] = now
            self._audit_event("auth_success", mac=mac, account=account)
            return dict(record)

    def record_traffic(self, mac, octets, now=0.0):
        with self._lock:
            key = self._mac_index.get(mac)
            if key is None:
                return
            record = self._sessions[key]
            record["octets"] += octets
            record["last_activity"] = now

    def idle_check(self, now):
        with self._lock:
            idle_macs = [
                record["mac"]
                for record in self._sessions.values()
                if now - record["last_activity"] > self.idle_timeout
            ]
            for mac in idle_macs:
                key = self._mac_index.get(mac)
                if key is not None:
                    self._release(key)
                    self._audit_event("idle_disconnect", mac=mac)
            return idle_macs

    def set_mtu(self, mac, requested):
        with self._lock:
            if mac not in self._mac_index:
                raise PppoeError("ENOENT", "unknown mac: %s" % mac)
            if requested < 1492:
                self._audit_event("mtu_rejected", mac=mac, requested=requested)
                raise PppoeError("EMTU",
                                  "mtu %d below PPPoE minimum 1492" % requested)
            self._mtu[mac] = requested
            return requested

    def open_handles(self):
        with self._lock:
            return len(self._handles)

    def accept_discovery(self, mac, now=0.0):
        with self._lock:
            window = int(now)
            state = self._discovery.get(mac)
            if state is None or state[0] != window:
                state = [window, 0]
                self._discovery[mac] = state
            if state[1] >= self.discovery_pps:
                self._audit_event("discovery_rate_limited", mac=mac)
                raise PppoeError("ERATELIMIT",
                                  "discovery rate exceeded for mac: %s" % mac)
            state[1] += 1
            return True

    def normalize_service(self, name):
        return name.lower()

    def bulk_disconnect(self, macs, *, fail_stop=(), now=0.0):
        with self._lock:
            disconnected = []
            retry = []
            failed = set(fail_stop)
            for mac in macs:
                if mac in failed:
                    if mac not in self._retry_queue:
                        self._retry_queue.append(mac)
                    retry.append(mac)
                    self._audit_event("stop_accounting_failed", mac=mac)
                    continue
                key = self._mac_index.get(mac)
                if key is None:
                    continue
                self._release(key)
                disconnected.append(mac)
                self._audit_event("bulk_disconnect", mac=mac)
            return {"disconnected": disconnected, "retry": retry}

    def retry_accounting(self):
        with self._lock:
            pending = list(self._retry_queue)
            self._retry_queue = []
            remaining = []
            for mac in pending:
                key = self._mac_index.get(mac)
                if key is None:
                    continue
                self._release(key)
                self._audit_event("stop_accounting_retry_ok", mac=mac)
            return {"retry": remaining}

    def snapshot(self):
        with self._lock:
            return {
                "version": 1,
                "epoch": self._epoch,
                "sessions": [dict(record) for record in self._sessions.values()],
                "billing": dict(self._billing),
                "failures": dict(self._failures),
                "locked": list(self._locked),
                "retry_queue": list(self._retry_queue),
            }

    def restore(self, snapshot):
        with self._lock:
            self._sessions = {}
            self._mac_index = {}
            self._account_index = {}
            self._handles = set()
            self._mtu = {}
            self._discovery = {}
            self._epoch = snapshot.get("epoch", 0)
            self._billing = dict(snapshot.get("billing", {}))
            self._failures = dict(snapshot.get("failures", {}))
            self._locked = set(snapshot.get("locked", ()))
            self._retry_queue = list(snapshot.get("retry_queue", ()))
            for saved in snapshot.get("sessions", ()):
                record = dict(saved)
                key = (record["mac"], record["session_id"])
                self._sessions[key] = record
                self._mac_index[record["mac"]] = key
                self._account_index.setdefault(record["account"], set()).add(key)
                self._handles.add(record["handle"])
            self._audit_event("restore", sessions=len(self._sessions))

    def audit_events(self):
        with self._lock:
            return list(self._audit)
