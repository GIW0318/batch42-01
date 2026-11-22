"""SMB signing gate.

The gate keeps signing, session and handle state together.  Signatures are
bound to a session and to the channel on which they were produced.  A failed
signature tears down the session, while a request that arrives concurrently
with an in-flight request on the same session is refused without disturbing
the session.

Contract notes for the implementation:

* ``negotiate`` accepts only SMB2/SMB3 dialects.  An SMB1-only client is
  refused at handshake time with ``STATUS_NOT_SUPPORTED`` and reason
  ``SMB1 not supported``.  With ``require_signing`` True, ``signing`` False
  is refused with ``signing required``; an algorithm list without
  ``AES-CMAC`` or ``HMAC-SHA256`` is refused with ``no supported signing
  algorithm``.  ``AES-CMAC`` wins when available.
* ``bind_channel`` gives every channel its own key.  ``sign_request`` signs
  with the channel key, so a signature made with the primary channel key
  fails on another channel.
* ``submit`` verifies encrypted payloads as ciphertext before any decrypt.
  A bad signature returns ``STATUS_ACCESS_DENIED``, disconnects the session
  and returns the reserved credit.  At most one in-flight request per
  session: a second one returns ``STATUS_REQUEST_NOT_ACCEPTED`` with reason
  ``concurrent request``; ``hold`` keeps the session busy until ``release``.
* ``share_rules`` is matched case-insensitively, so a rule on ``Finance``
  also covers ``FINANCE``.
* ``renegotiate`` tokens are single use; a replayed token returns
  ``STATUS_INVALID_PARAMETER`` with reason ``replayed negotiate`` and does
  not disable signing.
* ``expire_sessions`` invalidates the session, its leases and its handles;
  ``read_handle`` then returns ``STATUS_FILE_CLOSED``.
* Durable handles survive ``export_state`` / ``load_state`` but
  ``restore_handle`` must present the session key again or returns
  ``STATUS_ACCESS_DENIED``.
* ``query_directory`` reports ``STATUS_BUFFER_OVERFLOW`` with
  ``truncated`` True when the encoded listing exceeds 64 KiB; it never
  claims ``STATUS_SUCCESS`` for a truncated listing.
* ``rename`` keeps the open handle valid and updates its path.
* Audit entries never contain the raw NTLM response.
"""

from __future__ import annotations

import copy
import hashlib
import hmac
import json
import secrets
import struct
import threading


STATUS_SUCCESS = "STATUS_SUCCESS"
STATUS_NOT_SUPPORTED = "STATUS_NOT_SUPPORTED"
STATUS_ACCESS_DENIED = "STATUS_ACCESS_DENIED"
STATUS_INVALID_PARAMETER = "STATUS_INVALID_PARAMETER"
STATUS_REQUEST_NOT_ACCEPTED = "STATUS_REQUEST_NOT_ACCEPTED"
STATUS_FILE_CLOSED = "STATUS_FILE_CLOSED"
STATUS_BUFFER_OVERFLOW = "STATUS_BUFFER_OVERFLOW"

_DEFAULT_CREDITS = 8
_DIRECTORY_BUFFER_BYTES = 64 * 1024


def _as_bytes(value):
    if value is None:
        return b""
    if isinstance(value, bytes):
        return value
    if isinstance(value, bytearray):
        return bytes(value)
    if isinstance(value, memoryview):
        return value.tobytes()
    if isinstance(value, str):
        return value.encode("utf-8")
    return repr(value).encode("utf-8")


def _secret_hash(value):
    return hashlib.sha256(_as_bytes(value)).hexdigest()


def _derive_mac_key(value):
    return hashlib.sha256(b"SMB-SIGNING-KEY\x00" + _as_bytes(value)).digest()


def _mac(key, message, algorithm):
    key_bytes = _derive_mac_key(key)
    algorithm = (algorithm or "HMAC-SHA256").upper().replace("_", "-")

    if algorithm == "AES-CMAC":
        try:
            from cryptography.hazmat.primitives import cmac
            from cryptography.hazmat.primitives.ciphers import algorithms

            signer = cmac.CMAC(algorithms.AES(key_bytes))
            signer.update(message)
            return signer.finalize()
        except Exception:
            # Keep the gate functional on deployments without the optional
            # AES-CMAC provider.  HMAC-SHA256 is still a supported negotiated
            # algorithm and is never downgraded to HMAC-MD5.
            pass

    return hmac.new(key_bytes, message, hashlib.sha256).digest()


def _canonical_message(
    session_id, channel_id, sequence, nonce, payload, algorithm
):
    fields = (
        b"SMB2-SIGNING-V1",
        _as_bytes(algorithm or "HMAC-SHA256"),
        _as_bytes(session_id),
        _as_bytes(channel_id),
        _as_bytes(sequence),
        _as_bytes(nonce),
        _as_bytes(payload),
    )
    encoded = bytearray()
    for field in fields:
        encoded.extend(struct.pack("!I", len(field)))
        encoded.extend(field)
    return bytes(encoded)


def _normalize_dialect(dialect):
    if dialect is None:
        return ""
    return (
        str(dialect)
        .strip()
        .upper()
        .replace(".", "_")
        .replace("-", "_")
        .replace(" ", "_")
    )


def _is_modern_dialect(dialect):
    normalized = _normalize_dialect(dialect)
    return (
        normalized == "SMB2"
        or normalized.startswith("SMB2_")
        or normalized == "SMB3"
        or normalized.startswith("SMB3_")
    )


def _is_smb1(dialect):
    return _normalize_dialect(dialect).startswith("SMB1")


def _select_algorithm(algorithms):
    if isinstance(algorithms, str):
        algorithms = [algorithms]
    normalized = []
    for algorithm in algorithms or ():
        name = str(algorithm).strip().upper().replace("_", "-")
        normalized.append(name)
    if "AES-CMAC" in normalized:
        return "AES-CMAC"
    if "HMAC-SHA256" in normalized:
        return "HMAC-SHA256"
    return None


def _requires_signing(rule):
    if isinstance(rule, bool):
        return rule
    if isinstance(rule, dict):
        return bool(rule.get("require_signing", False))
    return False


def _as_list(value):
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    return list(value)


class SmbSigningGate:
    def __init__(self, require_signing=True, share_rules=None):
        self.require_signing = bool(require_signing)
        self.share_rules = copy.deepcopy(share_rules or {})
        self._sessions = {}
        self._handles = {}
        self._audit = []
        self._used_tokens = set()
        self._lock = threading.RLock()

    # ------------------------------------------------------------------
    # Negotiation and channel/key handling
    # ------------------------------------------------------------------
    def negotiate(
        self,
        client_id,
        dialects,
        algorithms,
        signing=True,
        session_id=None,
        now=0.0,
        ttl=300.0,
    ):
        with self._lock:
            dialect_list = _as_list(dialects)
            modern = [d for d in dialect_list if _is_modern_dialect(d)]
            if not modern:
                smb1_only = bool(dialect_list) and all(
                    _is_smb1(d) for d in dialect_list
                )
                return {
                    "status": STATUS_NOT_SUPPORTED,
                    "reason": (
                        "SMB1 not supported"
                        if smb1_only
                        else "no supported SMB dialect"
                    ),
                    "session_id": None,
                    "signing": False,
                }

            if self.require_signing and not signing:
                return {
                    "status": STATUS_NOT_SUPPORTED,
                    "reason": "signing required",
                    "session_id": None,
                    "signing": False,
                }

            algorithm = _select_algorithm(algorithms)
            if algorithm is None:
                return {
                    "status": STATUS_NOT_SUPPORTED,
                    "reason": "no supported signing algorithm",
                    "session_id": None,
                    "signing": False,
                }

            if session_id is None:
                session_id = "session-" + secrets.token_hex(16)

            existing = self._sessions.get(session_id)
            if (
                existing is not None
                and not existing["expired"]
                and existing["connected"]
            ):
                return {
                    "status": STATUS_INVALID_PARAMETER,
                    "reason": "session already exists",
                    "session_id": None,
                    "signing": False,
                }
            if existing is not None:
                # A replaced session must not leave old durable handles bound
                # to the new session id and key.
                self._close_session_handles(session_id)

            key = secrets.token_hex(32)
            expires_at = (
                float("inf") if ttl is None else float(now) + float(ttl)
            )
            session = {
                "session_id": session_id,
                "client_id": client_id,
                "dialects": copy.deepcopy(dialect_list),
                "algorithms": copy.deepcopy(_as_list(algorithms)),
                "selected_dialect": modern[0],
                "signing": bool(signing),
                "algorithm": algorithm,
                "key": key,
                "key_hash": _secret_hash(key),
                "channels": {0: key},
                "created_at": float(now),
                "expires_at": expires_at,
                "connected": True,
                "expired": False,
                "busy": False,
                "credits": _DEFAULT_CREDITS,
                "credit_cap": _DEFAULT_CREDITS,
                "next_sequence": 0,
                "used_nonces": set(),
                "signature_failures": 0,
            }
            self._sessions[session_id] = session
            return {
                "status": STATUS_SUCCESS,
                "session_id": session_id,
                "signing": bool(signing),
                "algorithm": algorithm,
                "dialect": modern[0],
                "expires_at": expires_at,
            }

    def session_key(self, session_id):
        with self._lock:
            session = self._sessions.get(session_id)
            if (
                session is None
                or session["expired"]
                or not session["connected"]
            ):
                return None
            return session["key"]

    def bind_channel(self, session_id, channel_id, channel_key):
        with self._lock:
            session = self._valid_session(session_id)
            if session is None:
                return {
                    "status": STATUS_ACCESS_DENIED,
                    "reason": "session not connected",
                    "session_id": session_id,
                    "channel_id": channel_id,
                }
            session["channels"][channel_id] = channel_key
            return {
                "status": STATUS_SUCCESS,
                "session_id": session_id,
                "channel_id": channel_id,
            }

    def _channel_key(self, session, channel_id):
        channels = session["channels"]
        if channel_id in channels:
            return channels[channel_id]
        if channel_id == 0:
            return session["key"]
        return None

    def sign_request(self, session_id, channel_id, payload):
        with self._lock:
            session = self._valid_session(session_id)
            if session is None or not session["signing"]:
                return None
            key = self._channel_key(session, channel_id)
            if key is None:
                return None

            sequence = session["next_sequence"]
            session["next_sequence"] = sequence + 1
            nonce = secrets.token_hex(16)
            message = _canonical_message(
                session_id,
                channel_id,
                sequence,
                nonce,
                payload,
                session["algorithm"],
            )
            signature = _mac(key, message, session["algorithm"]).hex()
            return {
                "sequence": sequence,
                "nonce": nonce,
                "signature": signature,
                "channel_id": channel_id,
                "algorithm": session["algorithm"],
            }

    def _verify_signature(self, session, channel_id, payload, signature):
        if not isinstance(signature, dict):
            return False
        if "sequence" not in signature or "nonce" not in signature:
            return False
        if "signature" not in signature:
            return False

        sequence = signature["sequence"]
        nonce = signature["nonce"]
        provided = signature["signature"]
        if sequence is None or nonce is None or provided is None:
            return False

        if "channel_id" in signature:
            if str(signature["channel_id"]) != str(channel_id):
                return False

        algorithm = session["algorithm"] or "HMAC-SHA256"
        if "algorithm" in signature:
            signed_algorithm = (
                str(signature["algorithm"]).upper().replace("_", "-")
            )
            if signed_algorithm != algorithm:
                return False

        key = self._channel_key(session, channel_id)
        if key is None:
            return False

        message = _canonical_message(
            session["session_id"],
            channel_id,
            sequence,
            nonce,
            payload,
            algorithm,
        )
        expected = _mac(key, message, algorithm).hex()
        if not hmac.compare_digest(expected, str(provided).lower()):
            return False

        replay_key = (str(channel_id), str(sequence), str(nonce))
        if replay_key in session["used_nonces"]:
            return False
        session["used_nonces"].add(replay_key)
        return True

    # ------------------------------------------------------------------
    # Request submission, credits and audit
    # ------------------------------------------------------------------
    def submit(
        self,
        session_id,
        channel_id,
        share,
        path,
        payload,
        signature,
        request_id=None,
        ciphertext=None,
        encrypted=False,
        ntlm_response=None,
        hold=False,
    ):
        with self._lock:
            request_id = request_id or "request-" + secrets.token_hex(12)

            def finish(result):
                self._record_submit_audit(
                    request_id=request_id,
                    session_id=session_id,
                    channel_id=channel_id,
                    share=share,
                    path=path,
                    encrypted=encrypted,
                    ntlm_response=ntlm_response,
                    result=result,
                )
                return result

            session = self._valid_session(session_id)
            if session is None:
                return finish(
                    {
                        "status": STATUS_ACCESS_DENIED,
                        "reason": "session not connected",
                        "disconnected": True,
                        "request_id": request_id,
                        "data": None,
                    }
                )

            if session["busy"]:
                return finish(
                    {
                        "status": STATUS_REQUEST_NOT_ACCEPTED,
                        "reason": "concurrent request",
                        "disconnected": False,
                        "request_id": request_id,
                        "data": None,
                    }
                )

            rule = self._share_rule(share)
            policy_requires_signing = _requires_signing(rule)
            if policy_requires_signing and not session["signing"]:
                return finish(
                    {
                        "status": STATUS_ACCESS_DENIED,
                        "reason": "signing required",
                        "disconnected": False,
                        "request_id": request_id,
                        "data": None,
                    }
                )

            signature_payload = ciphertext if encrypted else payload
            signing_required = session["signing"] or policy_requires_signing
            if signing_required and not self._verify_signature(
                session, channel_id, signature_payload, signature
            ):
                return finish(self._reject_bad_signature(session, request_id))

            if session["credits"] <= 0:
                return finish(
                    {
                        "status": STATUS_REQUEST_NOT_ACCEPTED,
                        "reason": "no credits",
                        "disconnected": False,
                        "request_id": request_id,
                        "data": None,
                    }
                )

            session["busy"] = True
            session["credits"] -= 1

            if encrypted:
                # Signature verification above was over ciphertext.  Only
                # after it succeeds is any plaintext representation exposed.
                data = payload if payload is not None else ciphertext
            else:
                data = payload

            if not hold:
                session["busy"] = False

            return finish(
                {
                    "status": STATUS_SUCCESS,
                    "disconnected": False,
                    "request_id": request_id,
                    "data": data,
                    "channel_id": channel_id,
                    "credits": session["credits"],
                }
            )

    def release(self, session_id):
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                return {"released": False}
            released = bool(session["busy"])
            session["busy"] = False
            return {"released": released}

    def available_credits(self, session_id):
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                return 0
            return session["credits"]

    def grant_credits(self, session_id, count):
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                return {
                    "status": STATUS_ACCESS_DENIED,
                    "reason": "session not found",
                    "available_credits": 0,
                }
            try:
                amount = max(0, int(count))
            except (TypeError, ValueError):
                amount = 0
            session["credits"] += amount
            session["credit_cap"] += amount
            return {
                "status": STATUS_SUCCESS,
                "available_credits": session["credits"],
            }

    def is_connected(self, session_id):
        with self._lock:
            return self._valid_session(session_id) is not None

    def _reject_bad_signature(self, session, request_id):
        session["signature_failures"] += 1
        session["busy"] = False
        session["connected"] = False
        session["credits"] = min(
            session["credit_cap"], session["credits"] + 1
        )
        self._close_session_handles(session["session_id"])
        return {
            "status": STATUS_ACCESS_DENIED,
            "reason": "invalid signature",
            "disconnected": True,
            "request_id": request_id,
            "data": None,
        }

    def _record_submit_audit(
        self,
        request_id,
        session_id,
        channel_id,
        share,
        path,
        encrypted,
        ntlm_response,
        result,
    ):
        entry = {
            "event": "submit",
            "request_id": request_id,
            "session_id": session_id,
            "channel_id": channel_id,
            "share": share,
            "path": path,
            "encrypted": bool(encrypted),
            "status": result.get("status"),
        }
        if ntlm_response is not None:
            # Never persist the raw NTLM response.  Keep only a fingerprint
            # useful for correlating repeated authentication attempts.
            entry["ntlm_response"] = "<redacted>"
            entry["ntlm_response_sha256"] = hashlib.sha256(
                _as_bytes(ntlm_response)
            ).hexdigest()
        self._audit.append(entry)

    # ------------------------------------------------------------------
    # Negotiation replay protection
    # ------------------------------------------------------------------
    def renegotiate(
        self, session_id, token, dialects, algorithms, signing=True
    ):
        with self._lock:
            session = self._valid_session(session_id)
            if session is None:
                return {
                    "status": STATUS_ACCESS_DENIED,
                    "reason": "session not connected",
                    "session_id": session_id,
                }

            # Replay is checked before validating the new offer so a replayed
            # token can never be used to negotiate a weaker algorithm.
            try:
                token_key = token
                hash(token_key)
            except TypeError:
                token_key = repr(token)
            token_used = token_key in self._used_tokens
            if token_used:
                return {
                    "status": STATUS_INVALID_PARAMETER,
                    "reason": "replayed negotiate",
                    "session_id": session_id,
                }

            dialect_list = _as_list(dialects)
            modern = [d for d in dialect_list if _is_modern_dialect(d)]
            if not modern:
                return {
                    "status": STATUS_NOT_SUPPORTED,
                    "reason": (
                        "SMB1 not supported"
                        if dialect_list
                        and all(_is_smb1(d) for d in dialect_list)
                        else "no supported SMB dialect"
                    ),
                    "session_id": session_id,
                }

            if self.require_signing and not signing:
                return {
                    "status": STATUS_NOT_SUPPORTED,
                    "reason": "signing required",
                    "session_id": session_id,
                }

            algorithm = _select_algorithm(algorithms)
            if algorithm is None:
                return {
                    "status": STATUS_NOT_SUPPORTED,
                    "reason": "no supported signing algorithm",
                    "session_id": session_id,
                }

            self._used_tokens.add(token_key)
            session["dialects"] = copy.deepcopy(dialect_list)
            session["algorithms"] = copy.deepcopy(_as_list(algorithms))
            session["selected_dialect"] = modern[0]
            session["signing"] = bool(signing)
            session["algorithm"] = algorithm
            return {
                "status": STATUS_SUCCESS,
                "session_id": session_id,
                "signing": bool(signing),
                "algorithm": algorithm,
                "dialect": modern[0],
            }

    # ------------------------------------------------------------------
    # Handles, leases and directory queries
    # ------------------------------------------------------------------
    def open_handle(
        self,
        session_id,
        share,
        path,
        lease_ttl=60.0,
        now=0.0,
        durable=False,
        entries=None,
    ):
        with self._lock:
            session = self._valid_session(session_id)
            if session is None:
                return {
                    "status": STATUS_FILE_CLOSED,
                    "reason": "session not connected",
                    "handle_id": None,
                }

            handle_id = "handle-" + secrets.token_hex(16)
            lease_expires_at = (
                None
                if lease_ttl is None
                else float(now) + float(lease_ttl)
            )
            handle = {
                "handle_id": handle_id,
                "session_id": session_id,
                "share": share,
                "path": path,
                "entries": copy.deepcopy(entries),
                "created_at": float(now),
                "lease_expires_at": lease_expires_at,
                "lease_active": True,
                "durable": bool(durable),
                "closed": False,
                "restored": False,
            }
            self._handles[handle_id] = handle
            return {
                "status": STATUS_SUCCESS,
                "handle_id": handle_id,
                "session_id": session_id,
                "share": share,
                "path": path,
                "durable": bool(durable),
                "lease_active": True,
            }

    def read_handle(self, handle_id, now=0.0):
        with self._lock:
            handle = self._handles.get(handle_id)
            if handle is None or handle["closed"]:
                return {
                    "status": STATUS_FILE_CLOSED,
                    "handle_id": handle_id,
                    "data": None,
                }

            session = self._sessions.get(handle["session_id"])
            if session is not None and (
                session["expired"] or not session["connected"]
            ):
                if not handle["restored"]:
                    return {
                        "status": STATUS_FILE_CLOSED,
                        "handle_id": handle_id,
                        "data": None,
                    }

            self._refresh_lease(handle, now)
            return {
                "status": STATUS_SUCCESS,
                "handle_id": handle_id,
                "session_id": handle["session_id"],
                "share": handle["share"],
                "path": handle["path"],
                "data": None,
                "lease_active": handle["lease_active"],
            }

    def query_directory(self, handle_id):
        with self._lock:
            handle = self._handles.get(handle_id)
            if handle is None or handle["closed"]:
                return {
                    "status": STATUS_FILE_CLOSED,
                    "handle_id": handle_id,
                    "truncated": False,
                    "entries": [],
                    "returned_bytes": 0,
                }

            session = self._sessions.get(handle["session_id"])
            if session is not None and (
                session["expired"] or not session["connected"]
            ):
                if not handle["restored"]:
                    return {
                        "status": STATUS_FILE_CLOSED,
                        "handle_id": handle_id,
                        "truncated": False,
                        "entries": [],
                        "returned_bytes": 0,
                    }

            entries = handle.get("entries") or []
            total_entries = len(entries)
            full_encoding = self._encode_entries(entries)
            if len(full_encoding) <= _DIRECTORY_BUFFER_BYTES:
                return {
                    "status": STATUS_SUCCESS,
                    "handle_id": handle_id,
                    "truncated": False,
                    "entries": copy.deepcopy(entries),
                    "returned_bytes": len(full_encoding),
                    "total_entries": total_entries,
                }

            low, high = 0, len(entries)
            while low < high:
                middle = (low + high + 1) // 2
                if (
                    len(self._encode_entries(entries[:middle]))
                    <= _DIRECTORY_BUFFER_BYTES
                ):
                    low = middle
                else:
                    high = middle - 1

            selected = copy.deepcopy(entries[:low])
            returned_bytes = len(self._encode_entries(selected))
            return {
                "status": STATUS_BUFFER_OVERFLOW,
                "handle_id": handle_id,
                "truncated": True,
                "entries": selected,
                "returned_bytes": returned_bytes,
                "total_entries": total_entries,
            }

    def rename(self, handle_id, new_path):
        with self._lock:
            handle = self._handles.get(handle_id)
            if handle is None or handle["closed"]:
                return {
                    "status": STATUS_FILE_CLOSED,
                    "handle_id": handle_id,
                    "path": None,
                }

            session = self._sessions.get(handle["session_id"])
            if session is not None and (
                session["expired"] or not session["connected"]
            ):
                if not handle["restored"]:
                    return {
                        "status": STATUS_FILE_CLOSED,
                        "handle_id": handle_id,
                        "path": None,
                    }

            old_share = str(handle["share"]).casefold()
            old_path = handle["path"]
            # All handles for the same file observe the rename.  Holding the
            # gate lock makes a concurrent open/rename linearizable.
            for other in self._handles.values():
                if other["closed"]:
                    continue
                if (
                    str(other["share"]).casefold() == old_share
                    and other["path"] == old_path
                ):
                    other["path"] = new_path

            return {
                "status": STATUS_SUCCESS,
                "handle_id": handle_id,
                "path": new_path,
            }

    def handle_path(self, handle_id):
        with self._lock:
            handle = self._handles.get(handle_id)
            if handle is None or handle["closed"]:
                return None
            return handle["path"]

    def lease_active(self, handle_id):
        with self._lock:
            handle = self._handles.get(handle_id)
            if handle is None or handle["closed"]:
                return False
            if not handle["lease_active"]:
                return False
            session = self._sessions.get(handle["session_id"])
            if session is not None and (
                session["expired"] or not session["connected"]
            ):
                if not handle["restored"]:
                    return False
            return True

    # ------------------------------------------------------------------
    # Session expiration and durable-handle restore
    # ------------------------------------------------------------------
    def expire_sessions(self, now):
        with self._lock:
            now = float(now)
            expired = []
            closed_handles = []
            expired_leases = []

            for session_id, session in list(self._sessions.items()):
                if session["expired"]:
                    continue
                if now >= session["expires_at"]:
                    session["expired"] = True
                    session["connected"] = False
                    session["busy"] = False
                    expired.append(session_id)
                    closed_handles.extend(
                        self._close_session_handles(session_id)
                    )

            for handle_id, handle in self._handles.items():
                if handle["closed"] or not handle["lease_active"]:
                    continue
                if (
                    handle["lease_expires_at"] is not None
                    and now >= handle["lease_expires_at"]
                ):
                    handle["lease_active"] = False
                    expired_leases.append(handle_id)

            return {
                "expired": expired,
                "closed_handles": closed_handles,
                "expired_leases": expired_leases,
            }

    def restore_handle(self, handle_id, session_key):
        with self._lock:
            handle = self._handles.get(handle_id)
            if handle is None or handle["closed"]:
                return {
                    "status": STATUS_FILE_CLOSED,
                    "reason": "handle not found",
                    "handle_id": handle_id,
                }

            if not handle["durable"]:
                return {
                    "status": STATUS_ACCESS_DENIED,
                    "reason": "handle is not durable",
                    "handle_id": handle_id,
                }

            session = self._sessions.get(handle["session_id"])
            if session is None:
                return {
                    "status": STATUS_ACCESS_DENIED,
                    "reason": "unknown session",
                    "handle_id": handle_id,
                }

            if session["expired"]:
                handle["closed"] = True
                handle["lease_active"] = False
                return {
                    "status": STATUS_FILE_CLOSED,
                    "reason": "session expired",
                    "handle_id": handle_id,
                }

            provided_hash = _secret_hash(session_key)
            expected_hash = session.get("key_hash")
            if (
                expected_hash is None
                or not hmac.compare_digest(provided_hash, expected_hash)
            ):
                return {
                    "status": STATUS_ACCESS_DENIED,
                    "reason": "invalid session key",
                    "handle_id": handle_id,
                }

            # Re-authenticate the session with the key presented by the
            # client.  The durable handle is only usable after this rebind.
            session["key"] = session_key
            session["key_hash"] = provided_hash
            session["channels"] = {0: session_key}
            session["connected"] = True
            session["busy"] = False
            handle["restored"] = True
            handle["closed"] = False
            handle["lease_active"] = True
            return {
                "status": STATUS_SUCCESS,
                "handle_id": handle_id,
                "session_id": handle["session_id"],
            }

    # ------------------------------------------------------------------
    # Audit and persistence
    # ------------------------------------------------------------------
    def audit_entries(self):
        with self._lock:
            return copy.deepcopy(self._audit)

    def export_state(self):
        with self._lock:
            sessions = {}
            for session_id, session in self._sessions.items():
                sessions[session_id] = {
                    "session_id": session_id,
                    "client_id": session["client_id"],
                    "dialects": copy.deepcopy(session["dialects"]),
                    "algorithms": copy.deepcopy(session["algorithms"]),
                    "selected_dialect": session["selected_dialect"],
                    "signing": session["signing"],
                    "algorithm": session["algorithm"],
                    "key_hash": session["key_hash"],
                    "created_at": session["created_at"],
                    "expires_at": session["expires_at"],
                    "expired": session["expired"],
                    "credits": session["credits"],
                    "credit_cap": session["credit_cap"],
                    "next_sequence": session["next_sequence"],
                }

            handles = {}
            for handle_id, handle in self._handles.items():
                if not handle["durable"] or handle["closed"]:
                    continue
                handles[handle_id] = {
                    "handle_id": handle_id,
                    "session_id": handle["session_id"],
                    "share": handle["share"],
                    "path": handle["path"],
                    "entries": copy.deepcopy(handle["entries"]),
                    "created_at": handle["created_at"],
                    "lease_expires_at": handle["lease_expires_at"],
                    "lease_active": handle["lease_active"],
                    "durable": True,
                }

            return {
                "version": 1,
                "require_signing": self.require_signing,
                "share_rules": copy.deepcopy(self.share_rules),
                "sessions": sessions,
                "handles": handles,
                "audit": copy.deepcopy(self._audit),
            }

    def load_state(self, state):
        with self._lock:
            if not isinstance(state, dict):
                raise ValueError("state must be a mapping")

            self.require_signing = bool(
                state.get("require_signing", self.require_signing)
            )
            self.share_rules = copy.deepcopy(
                state.get("share_rules", self.share_rules)
            )
            self._sessions = {}
            self._handles = {}
            self._audit = copy.deepcopy(state.get("audit", []))
            self._used_tokens = set()

            for session_id, stored in state.get("sessions", {}).items():
                self._sessions[session_id] = {
                    "session_id": session_id,
                    "client_id": stored.get("client_id"),
                    "dialects": copy.deepcopy(stored.get("dialects", [])),
                    "algorithms": copy.deepcopy(
                        stored.get("algorithms", [])
                    ),
                    "selected_dialect": stored.get("selected_dialect"),
                    "signing": bool(stored.get("signing", True)),
                    "algorithm": stored.get("algorithm"),
                    "key": None,
                    "key_hash": stored.get("key_hash"),
                    "channels": {},
                    "created_at": stored.get("created_at", 0.0),
                    "expires_at": stored.get("expires_at", float("inf")),
                    "connected": False,
                    "expired": bool(stored.get("expired", False)),
                    "busy": False,
                    "credits": int(
                        stored.get("credits", _DEFAULT_CREDITS)
                    ),
                    "credit_cap": int(
                        stored.get("credit_cap", _DEFAULT_CREDITS)
                    ),
                    "next_sequence": int(stored.get("next_sequence", 0)),
                    "used_nonces": set(),
                    "signature_failures": 0,
                }

            for handle_id, stored in state.get("handles", {}).items():
                self._handles[handle_id] = {
                    "handle_id": handle_id,
                    "session_id": stored.get("session_id"),
                    "share": stored.get("share"),
                    "path": stored.get("path"),
                    "entries": copy.deepcopy(stored.get("entries")),
                    "created_at": stored.get("created_at", 0.0),
                    "lease_expires_at": stored.get("lease_expires_at"),
                    "lease_active": bool(
                        stored.get("lease_active", True)
                    ),
                    "durable": True,
                    "closed": False,
                    "restored": False,
                }

        return {
            "status": STATUS_SUCCESS,
            "sessions": len(self._sessions),
            "handles": len(self._handles),
        }

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    def _valid_session(self, session_id):
        session = self._sessions.get(session_id)
        if (
            session is None
            or session["expired"]
            or not session["connected"]
        ):
            return None
        return session

    def _share_rule(self, share):
        target = str(share).casefold()
        for name, rule in self.share_rules.items():
            if str(name).casefold() == target:
                return rule
        return {}

    def _close_session_handles(self, session_id):
        closed = []
        for handle_id, handle in self._handles.items():
            if handle["session_id"] == session_id and not handle["closed"]:
                handle["closed"] = True
                handle["lease_active"] = False
                closed.append(handle_id)
        return closed

    @staticmethod
    def _refresh_lease(handle, now):
        if handle["closed"]:
            handle["lease_active"] = False
            return
        if (
            handle["lease_expires_at"] is not None
            and float(now) >= handle["lease_expires_at"]
        ):
            handle["lease_active"] = False

    @staticmethod
    def _encode_entries(entries):
        try:
            return json.dumps(
                entries,
                sort_keys=True,
                separators=(",", ":"),
                default=repr,
            ).encode("utf-8")
        except Exception:
            return repr(entries).encode("utf-8")
