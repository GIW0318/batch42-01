"""OAuth device-code flow gate keyed by user code, device code and client."""

from __future__ import annotations

import copy
import secrets
import threading
from typing import Any, Iterable


class DeviceCodeError(Exception):
    def __init__(self, code, message=""):
        self.code = code
        super().__init__(code if not message else "%s: %s" % (code, message))


_USER_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
_USER_CODE_LENGTH = 8


def _normalize_scope(scope: Any) -> tuple[str, ...]:
    if scope is None:
        return ()
    if isinstance(scope, str):
        values = scope.replace(",", " ").split()
    else:
        try:
            values = [str(item) for item in scope]
        except TypeError as exc:
            raise DeviceCodeError("invalid_scope") from exc

    result = []
    seen = set()
    for value in values:
        value = value.strip()
        if value and value not in seen:
            seen.add(value)
            result.append(value)
    return tuple(result)


def _scope_text(scope: Iterable[str]) -> str:
    return " ".join(scope)


class DeviceCodeGate:
    def __init__(self, interval=5, expires_in=600):
        if interval <= 0:
            raise ValueError("interval must be positive")
        if expires_in <= 0:
            raise ValueError("expires_in must be positive")

        self.interval = interval
        self.expires_in = expires_in
        self._lock = threading.RLock()
        self._clients: dict[str, dict[str, Any]] = {}
        self._by_user_code: dict[str, dict[str, Any]] = {}
        self._by_device_code: dict[str, dict[str, Any]] = {}
        self._audit: list[dict[str, Any]] = []

    def register_client(
        self,
        client_id,
        *,
        name="app",
        scopes=(),
        icon_url=None,
        icon=None,
    ):
        client_id = str(client_id or "")
        if not client_id:
            raise DeviceCodeError("invalid_client")

        icon_value = icon_url or icon or ""
        client = {
            "name": str(name),
            "scopes": list(_normalize_scope(scopes)),
            "icon": icon_value,
            "icon_url": icon_value,
        }
        with self._lock:
            self._clients[client_id] = client
        return copy.deepcopy(client)

    def start(self, client_id, scope, *, now=0.0):
        with self._lock:
            client = self._clients.get(str(client_id or ""))
            if client is None:
                raise DeviceCodeError("invalid_client")

            requested_scopes = _normalize_scope(scope)
            allowed_scopes = set(client["scopes"])
            if not set(requested_scopes).issubset(allowed_scopes):
                raise DeviceCodeError("invalid_scope")

            user_code = self._new_user_code()
            device_code = self._new_device_code()
            flow = {
                "user_code": user_code,
                "device_code": device_code,
                "client_id": str(client_id),
                "requested_scopes": list(requested_scopes),
                "requested_scope": _scope_text(requested_scopes),
                "status": "pending",
                "created_at": now,
                "expires_at": now + self.expires_in,
                "interval": self.interval,
                "last_poll": None,
                "user_id": None,
                "approved_scopes": [],
                "approved_scope": "",
                "approved_at": None,
                "denied_at": None,
            }
            self._by_user_code[user_code] = flow
            self._by_device_code[device_code] = flow
            self._record(
                "device_flow_started",
                flow,
                now=now,
                scope=flow["requested_scope"],
            )
            return {
                "user_code": user_code,
                "device_code": device_code,
                "expires_in": self.expires_in,
                "interval": self.interval,
                "verification_uri": "/device",
            }

    def describe(self, user_code, *, now=None):
        with self._lock:
            flow = self._flow_for_user_code(user_code)
            if flow is None:
                raise DeviceCodeError("invalid_grant")

            client = self._clients.get(flow["client_id"], {})
            status = flow["status"]
            if now is not None and now >= flow["expires_at"]:
                status = "expired"

            return {
                "user_code": flow["user_code"],
                "device_code": flow["device_code"],
                "client_id": flow["client_id"],
                "application": client.get("name", flow["client_id"]),
                "icon": client.get("icon", ""),
                "icon_url": client.get("icon_url", ""),
                "scope": flow["requested_scope"],
                "scopes": list(flow["requested_scopes"]),
                "status": status,
            }

    def approve(self, user_code, user_id, *, scope=None, now=0.0):
        with self._lock:
            flow = self._flow_for_user_code(user_code)
            if flow is None:
                raise DeviceCodeError("invalid_grant")
            if not user_id or not str(user_id).strip():
                raise DeviceCodeError("login_required")
            if now >= flow["expires_at"] or flow["status"] == "expired":
                self._mark_expired(flow, now)
                raise DeviceCodeError("expired")
            if flow["status"] == "approved":
                raise DeviceCodeError("already_approved")
            if flow["status"] == "denied":
                raise DeviceCodeError("denied")
            if flow["status"] != "pending":
                raise DeviceCodeError("invalid_grant")

            if scope is None:
                approved_scopes = tuple(flow["requested_scopes"])
            else:
                approved_scopes = _normalize_scope(scope)
                if not set(approved_scopes).issubset(flow["requested_scopes"]):
                    raise DeviceCodeError("invalid_scope")

            flow["status"] = "approved"
            flow["user_id"] = str(user_id)
            flow["approved_scopes"] = list(approved_scopes)
            flow["approved_scope"] = _scope_text(approved_scopes)
            flow["approved_at"] = now
            self._record(
                "device_flow_approved",
                flow,
                now=now,
                user_id=flow["user_id"],
                scope=flow["approved_scope"],
            )
            return {
                "status": "approved",
                "user_code": flow["user_code"],
                "client_id": flow["client_id"],
                "scope": flow["approved_scope"],
            }

    def deny(self, user_code, user_id, *, now=0.0):
        with self._lock:
            flow = self._flow_for_user_code(user_code)
            if flow is None:
                raise DeviceCodeError("invalid_grant")
            if not user_id or not str(user_id).strip():
                raise DeviceCodeError("login_required")
            if now >= flow["expires_at"] or flow["status"] == "expired":
                self._mark_expired(flow, now)
                raise DeviceCodeError("expired")
            if flow["status"] == "denied":
                raise DeviceCodeError("already_denied")
            if flow["status"] == "approved":
                raise DeviceCodeError("already_approved")
            if flow["status"] != "pending":
                raise DeviceCodeError("invalid_grant")

            flow["status"] = "denied"
            flow["user_id"] = str(user_id)
            flow["denied_at"] = now
            self._record(
                "device_flow_denied",
                flow,
                now=now,
                user_id=flow["user_id"],
                scope=flow["requested_scope"],
            )
            return {
                "status": "denied",
                "user_code": flow["user_code"],
                "client_id": flow["client_id"],
            }

    def poll(self, device_code, client_id, *, now=0.0, client_ip="0.0.0.0"):
        with self._lock:
            flow = self._by_device_code.get(device_code)
            if flow is None or flow["client_id"] != str(client_id or ""):
                raise DeviceCodeError("invalid_grant")

            if now >= flow["expires_at"] or flow["status"] == "expired":
                self._mark_expired(flow, now, client_ip=client_ip)
                return self._poll_result(flow, "expired")

            if flow["status"] == "denied":
                return self._poll_result(flow, "denied")

            last_poll = flow["last_poll"]
            if last_poll is not None and now - last_poll < flow["interval"]:
                flow["interval"] = flow["interval"] + 5
                flow["last_poll"] = now
                self._record(
                    "device_flow_slow_down",
                    flow,
                    now=now,
                    client_ip=client_ip,
                    scope=flow["requested_scope"],
                )
                return self._poll_result(flow, "slow_down")

            flow["last_poll"] = now

            if flow["status"] == "approved":
                response = self._poll_result(flow, "token")
                response.update(
                    {
                        "access_token": secrets.token_urlsafe(32),
                        "token_type": "Bearer",
                        "scope": flow["approved_scope"],
                        "expires_in": self.expires_in,
                    }
                )
                self._record(
                    "device_flow_token_issued",
                    flow,
                    now=now,
                    client_ip=client_ip,
                    user_id=flow["user_id"],
                    scope=flow["approved_scope"],
                )
                self._remove_flow(flow)
                return response

            if flow["status"] != "pending":
                raise DeviceCodeError("invalid_grant")

            self._record(
                "device_flow_polled",
                flow,
                now=now,
                client_ip=client_ip,
                scope=flow["requested_scope"],
            )
            return self._poll_result(flow, "pending")

    def snapshot(self):
        with self._lock:
            return {
                "version": 1,
                "interval": self.interval,
                "expires_in": self.expires_in,
                "clients": copy.deepcopy(self._clients),
                "flows": [copy.deepcopy(flow) for flow in self._by_device_code.values()],
                "audit": copy.deepcopy(self._audit),
            }

    def restore(self, snapshot):
        if not isinstance(snapshot, dict):
            raise ValueError("invalid snapshot")

        with self._lock:
            self.interval = snapshot.get("interval", self.interval)
            self.expires_in = snapshot.get("expires_in", self.expires_in)
            self._clients = copy.deepcopy(snapshot.get("clients", {}))
            self._by_user_code = {}
            self._by_device_code = {}

            for flow in snapshot.get("flows", []):
                restored_flow = copy.deepcopy(flow)
                restored_flow["requested_scopes"] = list(
                    restored_flow.get("requested_scopes", [])
                )
                restored_flow["approved_scopes"] = list(
                    restored_flow.get("approved_scopes", [])
                )
                restored_flow.setdefault("interval", self.interval)
                restored_flow.setdefault("last_poll", None)
                self._by_user_code[restored_flow["user_code"]] = restored_flow
                self._by_device_code[restored_flow["device_code"]] = restored_flow

            self._audit = copy.deepcopy(snapshot.get("audit", []))

    def audit_events(self):
        with self._lock:
            return copy.deepcopy(self._audit)

    def _new_user_code(self):
        while True:
            code = "".join(
                secrets.choice(_USER_CODE_ALPHABET)
                for _ in range(_USER_CODE_LENGTH)
            )
            if code not in self._by_user_code:
                return code

    def _new_device_code(self):
        while True:
            code = secrets.token_urlsafe(32)
            if code not in self._by_device_code:
                return code

    @staticmethod
    def _canonical_user_code(user_code):
        if not isinstance(user_code, str):
            return ""
        return "".join(char for char in user_code.upper() if char.isalnum())

    def _flow_for_user_code(self, user_code):
        return self._by_user_code.get(self._canonical_user_code(user_code))

    def _mark_expired(self, flow, now, *, client_ip=None):
        if flow["status"] != "expired":
            flow["status"] = "expired"
            self._record(
                "device_flow_expired",
                flow,
                now=now,
                client_ip=client_ip,
                scope=flow["requested_scope"],
            )

    def _remove_flow(self, flow):
        self._by_device_code.pop(flow["device_code"], None)
        if self._by_user_code.get(flow["user_code"]) is flow:
            self._by_user_code.pop(flow["user_code"], None)

    @staticmethod
    def _poll_result(flow, status):
        return {
            "status": status,
            "interval": flow["interval"],
            "scope": flow["approved_scope"]
            if status == "token"
            else flow["requested_scope"],
        }

    def _record(self, event, flow, *, now=0.0, client_ip=None, **extra):
        record = {
            "event": event,
            "timestamp": now,
            "user_code": flow["user_code"],
            "device_code": flow["device_code"],
            "client_id": flow["client_id"],
        }
        if client_ip is not None:
            record["client_ip"] = client_ip
        record.update(extra)
        self._audit.append(record)
