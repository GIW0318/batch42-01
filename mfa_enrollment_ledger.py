"""Thread-safe MFA enrollment ledger keyed by user, device and one-time code."""

import base64
import copy
import hashlib
import hmac
import secrets
import threading
import time


class MfaError(Exception):
    def __init__(self, code, message=""):
        self.code = code
        super().__init__(code if not message else "%s: %s" % (code, message))


class MfaEnrollmentLedger:
    def __init__(self, enforcement=True, totp_step=30, totp_window=1,
                 grace_days=7, max_confirm_attempts=5):
        self.enforcement = enforcement
        self.totp_step = totp_step
        self.totp_window = totp_window
        self.grace_days = grace_days
        self.max_confirm_attempts = max_confirm_attempts

        self._lock = threading.RLock()
        self._users = {}
        self._devices = {}
        self._consumed = {}
        self._backup_salt = secrets.token_bytes(16)
        self._backup_codes = {}
        self._audit = []

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    def _user(self, user):
        return self._users.setdefault(
            user,
            {"activated_at": None, "grace_used": False, "phone": None},
        )

    def _record_audit(self, action, user, actor=None, device_id=None, **extra):
        event = {
            "action": action,
            "user": user,
            "actor": actor,
            "device_id": device_id,
        }
        event.update(extra)
        self._audit.append(event)

    def _totp(self, secret, counter, digits=6):
        padding = "=" * ((8 - len(secret) % 8) % 8)
        key = base64.b32decode(secret + padding)
        digest = hmac.new(
            key, int(counter).to_bytes(8, "big"), hashlib.sha1
        ).digest()
        offset = digest[-1] & 0x0F
        value = int.from_bytes(digest[offset:offset + 4], "big") & 0x7FFFFFFF
        return str(value % (10 ** digits)).zfill(digits)

    def _matching_counter(self, secret, code, now):
        if self.totp_step <= 0:
            return None
        current = int(now // self.totp_step)
        window = max(0, int(self.totp_window))
        code = str(code).strip()
        for counter in range(current - window, current + window + 1):
            if counter < 0:
                continue
            if hmac.compare_digest(self._totp(secret, counter), code):
                return counter
        return None

    def _hash_backup_code(self, code):
        return hmac.new(
            self._backup_salt, str(code).encode("utf-8"), hashlib.sha256
        ).hexdigest()

    def _is_consumed(self, user, device_id, code):
        return str(code).strip() in self._consumed.get(user, {}).get(
            device_id, set()
        )

    def _consume(self, user, device_id, code):
        devices = self._consumed.setdefault(user, {})
        devices.setdefault(device_id, set()).add(str(code).strip())

    # ------------------------------------------------------------------
    # Enrollment
    # ------------------------------------------------------------------
    def activate_user(self, user, *, now=0.0):
        with self._lock:
            state = self._user(user)
            if state["activated_at"] is None:
                state["activated_at"] = float(now)
                state["grace_used"] = True

    def begin_enrollment(self, user, device_id, *, actor=None):
        with self._lock:
            self._user(user)
            devices = self._devices.setdefault(user, {})

            for record in devices.values():
                if record["state"] == "active":
                    raise MfaError(
                        "EALREADY",
                        "user already has an active authenticator",
                    )

            existing = devices.get(device_id)
            if existing is not None:
                if existing["state"] == "pending":
                    return self._enrollment_info(user, device_id, existing)
                raise MfaError("EALREADY", "device is already enrolled")

            for other_id, record in devices.items():
                if other_id != device_id and record["state"] == "pending":
                    raise MfaError(
                        "EALREADY",
                        "another enrollment is already pending",
                    )

            record = {
                "secret": base64.b32encode(secrets.token_bytes(20))
                .decode("ascii")
                .rstrip("="),
                "state": "pending",
                "confirm_attempts": 0,
                "actor": actor,
            }
            devices[device_id] = record
            self._record_audit(
                "begin_enrollment", user, actor=actor, device_id=device_id
            )
            return self._enrollment_info(user, device_id, record)

    def _enrollment_info(self, user, device_id, record):
        return {
            "user": user,
            "device_id": device_id,
            "secret": record["secret"],
            "state": record["state"],
        }

    def confirm_enrollment(self, user, device_id, code, *, now=0.0, actor=None):
        with self._lock:
            record = self._devices.get(user, {}).get(device_id)
            if record is None or record["state"] not in ("pending", "active"):
                raise MfaError("UNKNOWN_DEVICE", "unknown device")
            if record["state"] == "active":
                raise MfaError("EALREADY", "device is already active")
            if record["confirm_attempts"] >= self.max_confirm_attempts:
                raise MfaError("ELOCKED", "too many confirmation attempts")

            for other_id, other in self._devices.get(user, {}).items():
                if other_id != device_id and other["state"] == "active":
                    raise MfaError(
                        "EALREADY",
                        "user already has an active authenticator",
                    )

            normalized = str(code).strip()
            if self._is_consumed(user, device_id, normalized):
                raise MfaError("REPLAY", "code has already been used")

            counter = self._matching_counter(
                record["secret"], normalized, float(now)
            )
            if counter is None:
                record["confirm_attempts"] += 1
                raise MfaError("INVALID_CODE", "invalid confirmation code")

            record["state"] = "active"

            # A completed enrollment supersedes any stale pending flow.
            for other_id, other in list(self._devices[user].items()):
                if other_id != device_id and other["state"] == "pending":
                    del self._devices[user][other_id]

            event_actor = actor if actor is not None else record.get("actor")
            self._record_audit(
                "confirm_enrollment",
                user,
                actor=event_actor,
                device_id=device_id,
            )
            return {
                "user": user,
                "device_id": device_id,
                "state": "active",
            }

    def enrollments(self, user):
        with self._lock:
            result = []
            for device_id, record in self._devices.get(user, {}).items():
                if record["state"] == "active":
                    result.append({
                        "user": user,
                        "device_id": device_id,
                        "state": "active",
                    })
            return result

    # ------------------------------------------------------------------
    # Verification
    # ------------------------------------------------------------------
    def verify(self, user, code, *, device_id=None, now=0.0):
        with self._lock:
            devices = self._devices.get(user, {})
            if device_id is not None:
                record = devices.get(device_id)
                if record is None or record["state"] != "active":
                    raise MfaError("UNKNOWN_DEVICE", "unknown device")
                candidates = [(device_id, record)]
            else:
                candidates = [
                    (did, record)
                    for did, record in devices.items()
                    if record["state"] == "active"
                ]
                if not candidates:
                    raise MfaError("UNKNOWN_DEVICE", "no active device")

            normalized = str(code).strip()
            for did, record in candidates:
                if self._is_consumed(user, did, normalized):
                    raise MfaError("REPLAY", "code has already been used")

            for did, record in candidates:
                counter = self._matching_counter(
                    record["secret"], normalized, float(now)
                )
                if counter is not None:
                    self._consume(user, did, normalized)
                    return {
                        "ok": True,
                        "user": user,
                        "device_id": did,
                        "counter": counter,
                    }

            raise MfaError("INVALID_CODE", "invalid code")

    def remove_device(self, user, device_id, *, actor=None):
        with self._lock:
            devices = self._devices.get(user, {})
            if device_id not in devices:
                raise MfaError("UNKNOWN_DEVICE", "unknown device")
            del devices[device_id]
            if not devices:
                self._devices.pop(user, None)
            self._record_audit(
                "remove_device", user, actor=actor, device_id=device_id
            )
            return True

    # ------------------------------------------------------------------
    # Backup codes
    # ------------------------------------------------------------------
    def generate_backup_codes(self, user, *, actor=None):
        with self._lock:
            self._user(user)
            codes = []
            while len(codes) < 10:
                candidate = secrets.token_hex(5).upper()
                if candidate not in codes:
                    codes.append(candidate)
            self._backup_codes[user] = [
                {"hash": self._hash_backup_code(code), "used": False}
                for code in codes
            ]
            self._record_audit(
                "generate_backup_codes", user, actor=actor
            )
            return codes

    def use_backup_code(self, user, code):
        with self._lock:
            normalized = str(code).strip()
            digest = self._hash_backup_code(normalized)
            for entry in self._backup_codes.get(user, []):
                if not entry["used"] and hmac.compare_digest(
                    entry["hash"], digest
                ):
                    entry["used"] = True
                    self._record_audit("use_backup_code", user)
                    return True
            return False

    # ------------------------------------------------------------------
    # Phone / recovery
    # ------------------------------------------------------------------
    def set_phone(self, user, phone):
        with self._lock:
            state = self._user(user)
            state["phone"] = phone
            self._record_audit("set_phone", user)

    def change_phone(self, user, phone):
        with self._lock:
            state = self._user(user)
            old_phone = state["phone"]
            state["phone"] = phone
            if old_phone is not None and old_phone != phone:
                # A phone change is a device change: the old authenticator
                # secret must stop producing valid codes immediately.
                self._devices.pop(user, None)
            self._record_audit("change_phone", user, old_phone=old_phone)

    def backup_phone(self, user):
        with self._lock:
            state = self._users.get(user)
            return None if state is None else state["phone"]

    def can_send_to(self, user, phone):
        with self._lock:
            state = self._users.get(user)
            return state is not None and state["phone"] == phone

    def recover(self, actor, user, *, id_number, mfa_ok=False):
        with self._lock:
            if not mfa_ok:
                raise MfaError(
                    "MFA_REQUIRED", "recovery requires MFA"
                )
            self._user(user)
            self._devices.pop(user, None)
            self._backup_codes.pop(user, None)
            self._record_audit("recover", user, actor=actor)
            return True

    # ------------------------------------------------------------------
    # Grace period
    # ------------------------------------------------------------------
    def grace_active(self, user, now=0.0):
        with self._lock:
            if not self.enforcement:
                return False
            state = self._users.get(user)
            if state is None or state["activated_at"] is None:
                return False
            expires_at = state["activated_at"] + self.grace_days * 86400
            return float(now) < expires_at

    def reset_grace(self, user):
        with self._lock:
            state = self._user(user)
            if self.enforcement and (
                state["grace_used"] or state["activated_at"] is not None
            ):
                raise MfaError(
                    "GRACE_ALREADY_USED",
                    "grace period cannot be reset",
                )
            if not self.enforcement:
                state["activated_at"] = time.time()
                state["grace_used"] = True

    # ------------------------------------------------------------------
    # Persistence and audit
    # ------------------------------------------------------------------
    def snapshot(self):
        with self._lock:
            consumed = {
                user: {
                    device_id: sorted(codes)
                    for device_id, codes in devices.items()
                }
                for user, devices in self._consumed.items()
            }
            return copy.deepcopy({
                "version": 1,
                "enforcement": self.enforcement,
                "totp_step": self.totp_step,
                "totp_window": self.totp_window,
                "grace_days": self.grace_days,
                "max_confirm_attempts": self.max_confirm_attempts,
                "users": self._users,
                "devices": self._devices,
                "consumed": consumed,
                "backup_salt": self._backup_salt.hex(),
                "backup_codes": self._backup_codes,
                "audit": self._audit,
            })

    def restore(self, snapshot):
        with self._lock:
            if not isinstance(snapshot, dict):
                raise MfaError("INVALID_SNAPSHOT", "snapshot must be a dict")
            data = copy.deepcopy(snapshot)
            self.enforcement = data.get("enforcement", self.enforcement)
            self.totp_step = data.get("totp_step", self.totp_step)
            self.totp_window = data.get("totp_window", self.totp_window)
            self.grace_days = data.get("grace_days", self.grace_days)
            self.max_confirm_attempts = data.get(
                "max_confirm_attempts", self.max_confirm_attempts
            )
            self._users = data.get("users", {})
            self._devices = data.get("devices", {})
            self._consumed = {
                user: {
                    device_id: set(codes)
                    for device_id, codes in devices.items()
                }
                for user, devices in data.get("consumed", {}).items()
            }
            salt = data.get("backup_salt")
            self._backup_salt = (
                bytes.fromhex(salt)
                if salt is not None
                else secrets.token_bytes(16)
            )
            self._backup_codes = data.get("backup_codes", {})
            self._audit = data.get("audit", [])
            return self

    def audit_events(self):
        with self._lock:
            return copy.deepcopy(self._audit)
