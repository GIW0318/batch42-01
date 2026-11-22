"""NFSv4.1 lease, lock, delegation and layout state keeper.

Leases, file locks, delegations and pNFS layouts are kept in one state
machine guarded by a single re-entrant lock so that concurrent SETCLIENTID,
OPEN, RENEW and expiry reclamation can never observe or leave half-deleted
state.
"""

import threading
import uuid

NFS4ERR_GRACE = "NFS4ERR_GRACE"
NFS4ERR_ACCESS = "NFS4ERR_ACCESS"
NFS4ERR_BAD_STATEID = "NFS4ERR_BAD_STATEID"
NFS4ERR_CLID_INUSE = "NFS4ERR_CLID_INUSE"
NFS4ERR_STALE = "NFS4ERR_STALE"
NFS4ERR_RESOURCE = "NFS4ERR_RESOURCE"
NFS4ERR_DENIED = "NFS4ERR_DENIED"


class NfsLeaseError(Exception):
    def __init__(self, code, message=""):
        self.code = code
        super().__init__(code if not message else "%s: %s" % (code, message))


class NfsLeaseKeeper:
    def __init__(self, lease_seconds=90, grace_seconds=30, exports=(), max_states=10000):
        self.lease_seconds = lease_seconds
        self.grace_seconds = grace_seconds
        self.exports = [self.normalize_export(path) for path in exports]
        self.max_states = max_states
        self._clock = 0.0
        self._guard = threading.RLock()
        self._grace = False
        self._clients = {}
        self._states = {}
        self._locks = {}
        self._handles = set()
        self._stale_handles = set()
        self._closed_exports = set()
        self._state_seq = 0
        self._layout_seq = 0

    def advance(self, seconds):
        with self._guard:
            self._clock += float(seconds)
            return self._clock

    def start_grace(self):
        with self._guard:
            self._grace = True
            return True

    def end_grace(self):
        with self._guard:
            self._grace = False
            return True

    def in_grace(self):
        with self._guard:
            return self._grace

    def normalize_export(self, path):
        parts = []
        for part in str(path).split("/"):
            if part in ("", "."):
                continue
            if part == "..":
                if parts:
                    parts.pop()
                continue
            parts.append(part)
        return "/" + "/".join(parts)

    def authorize_export(self, path):
        normalized = self.normalize_export(path)
        with self._guard:
            for closed_path in self._closed_exports:
                if normalized == closed_path or normalized.startswith(closed_path + "/"):
                    raise NfsLeaseError(NFS4ERR_ACCESS, normalized)
            for export_path in self.exports:
                if normalized == export_path or normalized.startswith(export_path + "/"):
                    return True
        raise NfsLeaseError(NFS4ERR_ACCESS, normalized)

    def setclientid(self, client_id, owner):
        with self._guard:
            existing = self._clients.get(client_id)
            if existing is not None:
                raise NfsLeaseError(NFS4ERR_CLID_INUSE, str(client_id))
            token = "verifier-" + uuid.uuid4().hex
            self._clients[client_id] = {
                "owner": owner,
                "token": token,
                "confirmed": False,
            }
            return token

    def confirm(self, client_id, token):
        with self._guard:
            client = self._clients.get(client_id)
            if client is None or client["token"] != token:
                raise NfsLeaseError(NFS4ERR_BAD_STATEID, str(client_id))
            client["confirmed"] = True
            return True

    def open_file(self, client_id, file_handle, *, reclaim=False):
        with self._guard:
            now = self._clock
            if file_handle in self._stale_handles:
                raise NfsLeaseError(NFS4ERR_STALE, str(file_handle))
            if self._grace and not reclaim:
                raise NfsLeaseError(NFS4ERR_GRACE, str(file_handle))
            if len(self._states) >= self.max_states:
                raise NfsLeaseError(NFS4ERR_RESOURCE, str(file_handle))
            self._ensure_client(client_id)
            self._state_seq += 1
            state_id = "stateid-%08d" % self._state_seq
            self._states[state_id] = {
                "state_id": state_id,
                "client_id": client_id,
                "file_handle": file_handle,
                "expires_at": now + self.lease_seconds,
                "delegated": False,
                "layout_id": None,
                "locks": set(),
            }
            self._handles.add(file_handle)
            return state_id

    def renew(self, client_id, state_id, *, queue_delay=0, now=None):
        with self._guard:
            state = self._require_state(client_id, state_id)
            current = self._clock if now is None else float(now)
            effective = current - float(queue_delay)
            if effective >= state["expires_at"]:
                self._reclaim_state(state_id)
                raise NfsLeaseError(NFS4ERR_BAD_STATEID, state_id)
            state["expires_at"] = current + self.lease_seconds
            return {
                "state_id": state_id,
                "expires_at": state["expires_at"],
                "remaining": state["expires_at"] - current,
            }

    def delegate(self, client_id, state_id):
        with self._guard:
            state = self._require_state(client_id, state_id)
            state["delegated"] = True
            return True

    def delegreturn(self, client_id, state_id):
        with self._guard:
            state = self._require_state(client_id, state_id)
            state["delegated"] = False
            for lock_key in list(state["locks"]):
                self._locks.pop((state["file_handle"], lock_key), None)
            state["locks"].clear()
            return True

    def acquire_lock(self, client_id, state_id, lock_key):
        with self._guard:
            state = self._require_state(client_id, state_id)
            key = (state["file_handle"], lock_key)
            owner = self._locks.get(key)
            if owner is not None and owner != state_id:
                raise NfsLeaseError(NFS4ERR_DENIED, str(lock_key))
            self._locks[key] = state_id
            state["locks"].add(lock_key)
            return True

    def release_lock(self, client_id, state_id, lock_key):
        with self._guard:
            state = self._require_state(client_id, state_id)
            key = (state["file_handle"], lock_key)
            if self._locks.get(key) == state_id:
                del self._locks[key]
            state["locks"].discard(lock_key)
            return True

    def layoutget(self, client_id, state_id):
        with self._guard:
            state = self._require_state(client_id, state_id)
            if state["layout_id"] is None:
                self._layout_seq += 1
                state["layout_id"] = "layoutid-%08d" % self._layout_seq
            return {
                "layout_id": state["layout_id"],
                "state_id": state_id,
                "file_handle": state["file_handle"],
            }

    def expire(self, now=None):
        with self._guard:
            current = self._clock if now is None else float(now)
            expired = [
                state_id
                for state_id, state in self._states.items()
                if state["expires_at"] <= current
            ]
            for state_id in expired:
                self._reclaim_state(state_id)
            return expired

    def callback_timeout(self, client_id):
        with self._guard:
            for state in self._states.values():
                if state["client_id"] == client_id:
                    state["delegated"] = False
            return "none"

    def disconnect_client(self, client_id):
        with self._guard:
            self._clients.pop(client_id, None)
            state_ids = [
                state_id
                for state_id, state in self._states.items()
                if state["client_id"] == client_id
            ]
            for state_id in state_ids:
                self._reclaim_state(state_id)
            return state_ids

    def close_export(self, path):
        with self._guard:
            normalized = self.normalize_export(path)
            self._closed_exports.add(normalized)
            notified = {
                state["client_id"]
                for state in self._states.values()
                if state["file_handle"] in self._handles
            }
            state_ids = list(self._states.keys())
            for state_id in state_ids:
                self._reclaim_state(state_id)
            self._stale_handles.update(self._handles)
            return sorted(notified)

    def state_count(self, client_id=None):
        with self._guard:
            return self._count_states(client_id)

    def lock_count(self, client_id=None):
        with self._guard:
            return sum(
                len(state["locks"])
                for state in self._states.values()
                if client_id is None or state["client_id"] == client_id
            )

    def delegation_count(self, client_id=None):
        with self._guard:
            return sum(
                1
                for state in self._states.values()
                if state["delegated"]
                and (client_id is None or state["client_id"] == client_id)
            )

    def snapshot(self):
        with self._guard:
            return {
                "version": 1,
                "clock": self._clock,
                "lease_seconds": self.lease_seconds,
                "grace_seconds": self.grace_seconds,
                "max_states": self.max_states,
                "exports": list(self.exports),
                "closed_exports": sorted(self._closed_exports),
                "stale_handles": sorted(self._stale_handles),
                "grace": self._grace,
                "clients": {
                    client_id: dict(info)
                    for client_id, info in self._clients.items()
                },
                "states": [
                    {
                        "state_id": state["state_id"],
                        "client_id": state["client_id"],
                        "file_handle": state["file_handle"],
                        "expires_at": state["expires_at"],
                        "delegated": state["delegated"],
                        "layout_id": state["layout_id"],
                        "locks": sorted(state["locks"]),
                    }
                    for state in self._states.values()
                ],
                "locks": [
                    [file_handle, lock_key, state_id]
                    for (file_handle, lock_key), state_id in self._locks.items()
                ],
                "next_state_seq": self._state_seq,
                "next_layout_seq": self._layout_seq,
            }

    def restore(self, snapshot):
        with self._guard:
            self._clock = snapshot["clock"]
            self.lease_seconds = snapshot["lease_seconds"]
            self.grace_seconds = snapshot["grace_seconds"]
            self.max_states = snapshot["max_states"]
            self.exports = list(snapshot["exports"])
            self._closed_exports = set(snapshot["closed_exports"])
            self._stale_handles = set(snapshot["stale_handles"])
            self._grace = snapshot["grace"]
            self._clients = {
                client_id: dict(info)
                for client_id, info in snapshot["clients"].items()
            }
            self._states = {}
            for raw in snapshot["states"]:
                state = dict(raw)
                state["locks"] = set(raw["locks"])
                self._states[state["state_id"]] = state
            self._locks = {
                (file_handle, lock_key): state_id
                for file_handle, lock_key, state_id in snapshot["locks"]
            }
            self._handles = {
                state["file_handle"] for state in self._states.values()
            }
            self._state_seq = snapshot["next_state_seq"]
            self._layout_seq = snapshot["next_layout_seq"]
            return True

    def _ensure_client(self, client_id):
        if client_id not in self._clients:
            self._clients[client_id] = {
                "owner": None,
                "token": None,
                "confirmed": False,
            }

    def _require_state(self, client_id, state_id):
        state = self._states.get(state_id)
        if state is None or state["client_id"] != client_id:
            raise NfsLeaseError(NFS4ERR_BAD_STATEID, state_id)
        return state

    def _reclaim_state(self, state_id):
        state = self._states.pop(state_id, None)
        if state is None:
            return
        for lock_key in state["locks"]:
            self._locks.pop((state["file_handle"], lock_key), None)

    def _count_states(self, client_id):
        if client_id is None:
            return len(self._states)
        return sum(
            1
            for state in self._states.values()
            if state["client_id"] == client_id
        )
