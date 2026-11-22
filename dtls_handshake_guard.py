"""DTLS handshake guard.

The implementation keeps handshake state explicit and serializable.  Every
operation that changes connection state is performed while holding ``_lock``
so duplicate ClientHello messages cannot race each other into creating two
sessions.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import threading
import uuid
from typing import Any


class FragmentLimitExceeded(Exception):
    """Raised when a handshake fragment exceeds the reassembly budget."""


_STATE_VERSION = 1


def _b64e(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _b64d(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


def _json_safe(value: Any) -> Any:
    """Make state values safe for JSON persistence."""
    if isinstance(value, bytes):
        return {"__bytes__": _b64e(value)}
    if isinstance(value, tuple):
        return [_json_safe(item) for item in value]
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _json_restore(value: Any) -> Any:
    if isinstance(value, dict) and set(value) == {"__bytes__"}:
        return _b64d(value["__bytes__"])
    if isinstance(value, list):
        return [_json_restore(item) for item in value]
    if isinstance(value, dict):
        return {key: _json_restore(item) for key, item in value.items()}
    return value


class DtlsHandshakeGuard:
    def __init__(self, cookie_secret, max_fragment_bytes=65536, max_early_data=2):
        self._lock = threading.RLock()
        self._cookie_secret = self._coerce_secret(cookie_secret)
        self._max_fragment_bytes = int(max_fragment_bytes)
        self._max_early_data = int(max_early_data)
        if self._max_fragment_bytes < 0:
            raise ValueError("max_fragment_bytes must be non-negative")
        if self._max_early_data < 0:
            raise ValueError("max_early_data must be non-negative")

        self._sessions: dict[str, dict[str, Any]] = {}
        self._tuple_sessions: dict[tuple, str] = {}
        self._cookies: dict[str, dict[str, Any]] = {}

    @staticmethod
    def _coerce_secret(secret) -> bytes:
        if isinstance(secret, bytes):
            return secret
        if isinstance(secret, bytearray):
            return bytes(secret)
        return str(secret).encode("utf-8")

    @staticmethod
    def _tuple(value) -> tuple:
        if isinstance(value, tuple):
            return value
        if isinstance(value, list):
            return tuple(value)
        return (value,)

    @staticmethod
    def _session_key(secret: bytes, session_id: str) -> bytes:
        return hmac.new(
            secret, b"dtls-session-key\x00" + session_id.encode("utf-8"), hashlib.sha256
        ).digest()

    @staticmethod
    def _epoch_key(session: dict[str, Any], epoch: int) -> bytes:
        return hmac.new(
            session["key"],
            b"dtls-epoch-key\x00" + int(epoch).to_bytes(8, "big"),
            hashlib.sha256,
        ).digest()

    def _new_cookie(self, five_tuple, client_random, now) -> tuple[str, dict[str, Any]]:
        payload = {
            "v": _STATE_VERSION,
            "transport": _json_safe(five_tuple),
            "random": _json_safe(client_random),
            "nonce": secrets.token_urlsafe(16),
            "iat": now,
        }
        payload_bytes = json.dumps(
            payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True
        ).encode("utf-8")
        body = _b64e(payload_bytes)
        signature = hmac.new(
            self._cookie_secret,
            b"dtls-cookie\x00" + body.encode("ascii"),
            hashlib.sha256,
        ).digest()
        cookie = body + "." + _b64e(signature)
        record = {
            "transport": list(five_tuple),
            "client_random": _json_safe(client_random),
            "used": False,
            "created_at": now,
        }
        return cookie, record

    def _decode_cookie(self, cookie: str) -> dict[str, Any] | None:
        if not isinstance(cookie, str) or "." not in cookie:
            return None
        body, encoded_signature = cookie.rsplit(".", 1)
        try:
            supplied_signature = _b64d(encoded_signature)
            expected_signature = hmac.new(
                self._cookie_secret,
                b"dtls-cookie\x00" + body.encode("ascii"),
                hashlib.sha256,
            ).digest()
            if not hmac.compare_digest(supplied_signature, expected_signature):
                return None
            payload = json.loads(_b64d(body).decode("utf-8"))
            if not isinstance(payload, dict) or payload.get("v") != _STATE_VERSION:
                return None
            return payload
        except (ValueError, TypeError, json.JSONDecodeError):
            return None

    @staticmethod
    def _transport_matches(cookie_transport, five_tuple) -> bool:
        if not isinstance(cookie_transport, (list, tuple)):
            return False
        try:
            left = [_json_restore(item) for item in cookie_transport]
        except TypeError:
            return False
        return tuple(left) == tuple(five_tuple)

    def client_hello(self, five_tuple, hello, now=0.0):
        five_tuple = self._tuple(five_tuple)
        hello = hello or {}
        client_random = hello.get("client_random", "")
        cookie = hello.get("cookie")

        with self._lock:
            if cookie is None:
                cookie_value, record = self._new_cookie(five_tuple, client_random, now)
                self._cookies[cookie_value] = record
                return {
                    "action": "hello_verify_request",
                    "cookie": cookie_value,
                    "session_id": None,
                }

            payload = self._decode_cookie(cookie)
            record = self._cookies.get(cookie)
            if payload is None or record is None:
                return {
                    "action": "reject",
                    "reason": "invalid_cookie",
                    "session_id": None,
                }

            if (
                not self._transport_matches(payload.get("transport"), five_tuple)
                or not self._transport_matches(record.get("transport"), five_tuple)
            ):
                return {
                    "action": "reject",
                    "reason": "cookie_transport_mismatch",
                    "session_id": None,
                }

            if _json_safe(record.get("client_random")) != _json_safe(client_random):
                return {
                    "action": "reject",
                    "reason": "cookie_client_random_mismatch",
                    "session_id": None,
                }

            if record.get("used"):
                return {
                    "action": "reject",
                    "reason": "duplicate_client_hello",
                    "session_id": self._tuple_sessions.get(five_tuple),
                }

            existing = self._tuple_sessions.get(five_tuple)
            if existing is not None:
                return {
                    "action": "reject",
                    "reason": "duplicate_client_hello",
                    "session_id": existing,
                }

            session_id = uuid.uuid4().hex
            session = {
                "session_id": session_id,
                "five_tuple": five_tuple,
                "client_random": client_random,
                "cookie": cookie,
                "state": "handshaking",
                "socket_open": True,
                "rtt": 0.0,
                "early_rejections": 0,
                "current_epoch": 0,
                "epochs": {0: {"high": -1, "seen": set()}},
                "key": self._session_key(self._cookie_secret, session_id),
                "nonce_prefix": secrets.token_bytes(4),
                "nonce_counter": 0,
                "fragments": {},
                "fragment_buffered": 0,
            }
            record["used"] = True
            self._sessions[session_id] = session
            self._tuple_sessions[five_tuple] = session_id
            return {
                "action": "server_hello",
                "session_id": session_id,
                "cookie": cookie,
            }

    def session_count(self):
        with self._lock:
            return len(self._sessions)

    def session_id_for(self, five_tuple):
        with self._lock:
            return self._tuple_sessions.get(self._tuple(five_tuple))

    def _get_session(self, session_id) -> dict[str, Any]:
        try:
            return self._sessions[session_id]
        except KeyError as exc:
            raise KeyError("unknown session") from exc

    def finish_handshake(self, session_id):
        with self._lock:
            session = self._get_session(session_id)
            if session["state"] == "closed":
                return {"state": "closed", "established": False}
            session["state"] = "established"
            return {"state": "established", "established": True}

    def connection_state(self, session_id):
        with self._lock:
            session = self._sessions.get(session_id)
            return session["state"] if session is not None else None

    @staticmethod
    def _release_socket(session: dict[str, Any]) -> bool:
        was_open = bool(session.get("socket_open"))
        session["socket_open"] = False
        return was_open

    def application_data(self, session_id, seq, payload, now=0.0):
        with self._lock:
            session = self._get_session(session_id)
            if session["state"] == "closed":
                return {
                    "accepted": False,
                    "alert": "unexpected_message",
                    "closed": True,
                    "epoch": session["current_epoch"],
                }
            if session["state"] != "established":
                session["early_rejections"] += 1
                closed = False
                if session["early_rejections"] > self._max_early_data:
                    session["state"] = "closed"
                    self._release_socket(session)
                    closed = True
                return {
                    "accepted": False,
                    "alert": "unexpected_message",
                    "closed": closed,
                    "epoch": session["current_epoch"],
                }

            return {
                "accepted": True,
                "alert": None,
                "closed": False,
                "epoch": session["current_epoch"],
            }

    def early_data_rejections(self, session_id):
        with self._lock:
            session = self._sessions.get(session_id)
            return session["early_rejections"] if session is not None else 0

    def receive_record(self, session_id, epoch, seq, payload=b"", now=0.0):
        with self._lock:
            session = self._get_session(session_id)
            if session["state"] == "closed":
                return {
                    "accepted": False,
                    "replay": False,
                    "reason": "session_closed",
                    "high_water": self._current_high_water(session),
                }

            epoch = int(epoch)
            seq = int(seq)
            if epoch < session["current_epoch"]:
                return {
                    "accepted": False,
                    "replay": False,
                    "reason": "stale_epoch",
                    "high_water": self._current_high_water(session),
                }
            if epoch > session["current_epoch"]:
                return {
                    "accepted": False,
                    "replay": False,
                    "reason": "invalid_epoch",
                    "high_water": self._current_high_water(session),
                }

            window = session["epochs"].setdefault(epoch, {"high": -1, "seen": set()})
            if seq <= window["high"]:
                if seq in window["seen"]:
                    return {
                        "accepted": False,
                        "replay": True,
                        "reason": "replay",
                        "high_water": window["high"],
                    }
                window["seen"].add(seq)
                return {
                    "accepted": True,
                    "replay": False,
                    "reason": "out_of_order",
                    "high_water": window["high"],
                }

            window["high"] = seq
            window["seen"].add(seq)
            return {
                "accepted": True,
                "replay": False,
                "reason": "in_order",
                "high_water": window["high"],
            }

    @staticmethod
    def _current_high_water(session: dict[str, Any]) -> int:
        epoch = session["current_epoch"]
        return int(session["epochs"].get(epoch, {}).get("high", -1))

    def receive_high_water(self, session_id):
        with self._lock:
            session = self._get_session(session_id)
            return self._current_high_water(session)

    def key_update(self, session_id):
        with self._lock:
            session = self._get_session(session_id)
            if session["state"] == "closed":
                return {"epoch": session["current_epoch"], "closed": True}
            session["current_epoch"] += 1
            new_epoch = session["current_epoch"]
            session["epochs"].setdefault(new_epoch, {"high": -1, "seen": set()})
            return {"epoch": new_epoch, "closed": False}

    def current_epoch(self, session_id):
        with self._lock:
            return self._get_session(session_id)["current_epoch"]

    def protect(self, session_id, plaintext):
        if isinstance(plaintext, str):
            plaintext = plaintext.encode("utf-8")
        elif not isinstance(plaintext, (bytes, bytearray)):
            plaintext = bytes(plaintext)
        plaintext = bytes(plaintext)

        with self._lock:
            session = self._get_session(session_id)
            epoch = session["current_epoch"]
            counter = session["nonce_counter"]
            if counter >= 2**64:
                raise OverflowError("nonce counter exhausted")
            session["nonce_counter"] = counter + 1
            nonce = session["nonce_prefix"] + counter.to_bytes(8, "big")
            key = self._epoch_key(session, epoch)

            stream = bytearray()
            block = 0
            while len(stream) < len(plaintext):
                stream.extend(
                    hmac.new(
                        key,
                        b"dtls-keystream\x00"
                        + nonce
                        + block.to_bytes(4, "big"),
                        hashlib.sha256,
                    ).digest()
                )
                block += 1

            ciphertext = bytes(
                byte ^ stream[index] for index, byte in enumerate(plaintext)
            )
            tag = hmac.new(
                key, b"dtls-record-tag\x00" + nonce + ciphertext, hashlib.sha256
            ).digest()
            return {
                "epoch": epoch,
                "nonce": nonce,
                "ciphertext": ciphertext + tag,
            }

    def note_rtt(self, session_id, rtt):
        with self._lock:
            session = self._get_session(session_id)
            session["rtt"] = max(0.0, float(rtt))
            return session["rtt"]

    def retransmission_delay(self, session_id, attempt):
        with self._lock:
            session = self._get_session(session_id)
            attempt = max(0, int(attempt))
            base = max(1.0, 2.0 * float(session["rtt"]))
            if attempt >= 6:
                return 60.0
            return min(60.0, base * (2.0**attempt))

    def fragment(self, session_id, msg_seq, offset, total_len, data):
        if isinstance(data, str):
            data = data.encode("utf-8")
        else:
            data = bytes(data)

        try:
            offset = int(offset)
            total_len = int(total_len)
        except (TypeError, ValueError) as exc:
            raise ValueError("offset and total_len must be integers") from exc

        if total_len < 0 or offset < 0:
            raise ValueError("offset and total_len must be non-negative")
        if total_len > self._max_fragment_bytes:
            raise FragmentLimitExceeded("declared handshake message is too large")

        with self._lock:
            session = self._get_session(session_id)

            if offset + len(data) > total_len:
                if (
                    session["fragment_buffered"] + len(data)
                    > self._max_fragment_bytes
                ):
                    raise FragmentLimitExceeded(
                        "fragment reassembly budget exceeded"
                    )
                raise ValueError("fragment extends beyond declared total length")

            fragment = session["fragments"].get(msg_seq)
            if fragment is not None and fragment["total_len"] != total_len:
                raise ValueError("inconsistent total length for fragment sequence")

            if total_len == 0:
                return {
                    "complete": True,
                    "data": b"",
                    "buffered": session["fragment_buffered"],
                }
            if not data:
                return {
                    "complete": False,
                    "data": None,
                    "buffered": session["fragment_buffered"],
                }

            existing_chunks = [] if fragment is None else fragment["chunks"]
            pieces = [(offset, data)]
            for existing_start, existing_data in existing_chunks:
                existing_end = existing_start + len(existing_data)
                remaining = []
                for piece_start, piece_data in pieces:
                    piece_end = piece_start + len(piece_data)
                    if piece_end <= existing_start or piece_start >= existing_end:
                        remaining.append((piece_start, piece_data))
                        continue
                    if piece_start < existing_start:
                        remaining.append(
                            (
                                piece_start,
                                piece_data[: existing_start - piece_start],
                            )
                        )
                    if piece_end > existing_end:
                        remaining.append(
                            (existing_end, piece_data[existing_end - piece_start :])
                        )
                pieces = remaining

            new_bytes = sum(len(piece_data) for _, piece_data in pieces)
            if (
                session["fragment_buffered"] + new_bytes
                > self._max_fragment_bytes
            ):
                raise FragmentLimitExceeded("fragment reassembly budget exceeded")

            if fragment is not None and fragment["bytes"] + new_bytes > total_len:
                raise ValueError("fragment data exceeds declared total length")
            if fragment is None:
                fragment = {
                    "total_len": total_len,
                    "chunks": [],
                    "bytes": 0,
                }
                session["fragments"][msg_seq] = fragment

            fragment["chunks"].extend(pieces)
            fragment["bytes"] += new_bytes
            session["fragment_buffered"] += new_bytes
            if fragment["bytes"] != total_len:
                return {
                    "complete": False,
                    "data": None,
                    "buffered": session["fragment_buffered"],
                }

            complete_buffer = bytearray(total_len)
            for start, piece_data in fragment["chunks"]:
                complete_buffer[start : start + len(piece_data)] = piece_data
            complete_data = bytes(complete_buffer)
            session["fragment_buffered"] -= fragment["bytes"]
            del session["fragments"][msg_seq]
            return {
                "complete": True,
                "data": complete_data,
                "buffered": session["fragment_buffered"],
            }

    @staticmethod
    def _certificate_time_valid(cert, now: float) -> bool:
        try:
            return float(cert["not_before"]) <= now <= float(cert["not_after"])
        except (KeyError, TypeError, ValueError):
            return False

    def verify_chain(self, chain, now):
        if not isinstance(chain, (list, tuple)) or not chain:
            return {"valid": False, "reason": "incomplete_chain"}
        if not all(isinstance(cert, dict) for cert in chain):
            return {"valid": False, "reason": "incomplete_chain"}

        try:
            now = float(now)
        except (TypeError, ValueError):
            return {"valid": False, "reason": "incomplete_chain"}

        for cert in chain:
            if not all(key in cert for key in ("subject", "issuer", "ca")):
                return {"valid": False, "reason": "incomplete_chain"}
            if not self._certificate_time_valid(cert, now):
                return {"valid": False, "reason": "certificate_expired"}

        current = chain[0]
        used = {0}
        while current.get("subject") != current.get("issuer"):
            issuer_name = current.get("issuer")
            next_index = None
            for index, candidate in enumerate(chain):
                if index in used:
                    continue
                if candidate.get("subject") == issuer_name:
                    next_index = index
                    break
            if next_index is None:
                return {"valid": False, "reason": "incomplete_chain"}
            if not chain[next_index].get("ca"):
                return {"valid": False, "reason": "incomplete_chain"}
            used.add(next_index)
            current = chain[next_index]

        if not current.get("ca"):
            return {"valid": False, "reason": "incomplete_chain"}
        if len(used) != len(chain):
            return {"valid": False, "reason": "incomplete_chain"}
        return {"valid": True, "reason": None}

    def resume_session(self, ticket, chain, now):
        if not isinstance(ticket, dict) or ticket.get("signature") != "valid":
            return {
                "resumed": False,
                "reason": "invalid_signature",
                "session_id": None,
            }
        verdict = self.verify_chain(chain, now)
        if not verdict["valid"]:
            return {
                "resumed": False,
                "reason": verdict["reason"],
                "session_id": None,
            }
        return {
            "resumed": True,
            "reason": None,
            "session_id": ticket.get("session_id"),
        }

    def close(self, session_id):
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                return {
                    "state": "closed",
                    "socket_released": True,
                    "previous_state": None,
                }
            previous_state = session["state"]
            was_open = self._release_socket(session)
            session["state"] = "closed"
            return {
                "state": "closed",
                "socket_released": was_open,
                "previous_state": previous_state,
            }

    def socket_count(self):
        with self._lock:
            return sum(
                1 for session in self._sessions.values() if session.get("socket_open")
            )

    def export_state(self):
        with self._lock:
            cookies = {}
            for cookie, record in self._cookies.items():
                cookies[cookie] = {
                    "transport": _json_safe(record.get("transport", [])),
                    "client_random": _json_safe(record.get("client_random")),
                    "used": bool(record.get("used")),
                    "created_at": record.get("created_at", 0.0),
                }

            sessions = {}
            for session_id, session in self._sessions.items():
                fragments = []
                for msg_seq, fragment in session["fragments"].items():
                    fragments.append(
                        {
                            "msg_seq": _json_safe(msg_seq),
                            "total_len": fragment["total_len"],
                            "chunks": [
                                {
                                    "offset": offset,
                                    "data": _b64e(piece_data),
                                }
                                for offset, piece_data in fragment["chunks"]
                            ],
                            "bytes": fragment["bytes"],
                        }
                    )
                sessions[session_id] = {
                    "session_id": session_id,
                    "five_tuple": _json_safe(session["five_tuple"]),
                    "client_random": _json_safe(session["client_random"]),
                    "cookie": session["cookie"],
                    "state": session["state"],
                    "socket_open": bool(session["socket_open"]),
                    "rtt": session["rtt"],
                    "early_rejections": session["early_rejections"],
                    "current_epoch": session["current_epoch"],
                    "epochs": {
                        str(epoch): {
                            "high": window["high"],
                            "seen": sorted(window["seen"]),
                        }
                        for epoch, window in session["epochs"].items()
                    },
                    "key": _b64e(session["key"]),
                    "nonce_prefix": _b64e(session["nonce_prefix"]),
                    "nonce_counter": session["nonce_counter"],
                    "fragments": fragments,
                    "fragment_buffered": session["fragment_buffered"],
                }

            return {
                "version": _STATE_VERSION,
                "cookie_secret": _b64e(self._cookie_secret),
                "cookies": cookies,
                "sessions": sessions,
            }

    def load_state(self, state):
        if not isinstance(state, dict) or state.get("version") != _STATE_VERSION:
            raise ValueError("unsupported or invalid DTLS guard state")

        try:
            secret = _b64d(state["cookie_secret"])
            raw_cookies = state.get("cookies", {})
            raw_sessions = state.get("sessions", {})
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("invalid DTLS guard state") from exc

        cookies: dict[str, dict[str, Any]] = {}
        for cookie, record in raw_cookies.items():
            if not isinstance(record, dict):
                raise ValueError("invalid cookie state")
            cookies[cookie] = {
                "transport": _json_restore(record.get("transport", [])),
                "client_random": _json_restore(record.get("client_random")),
                "used": bool(record.get("used")),
                "created_at": record.get("created_at", 0.0),
            }

        sessions: dict[str, dict[str, Any]] = {}
        tuple_sessions: dict[tuple, str] = {}
        for session_id, raw in raw_sessions.items():
            if not isinstance(raw, dict):
                raise ValueError("invalid session state")
            try:
                five_tuple = tuple(_json_restore(raw["five_tuple"]))
                epochs = {
                    int(epoch): {
                        "high": int(window.get("high", -1)),
                        "seen": set(int(value) for value in window.get("seen", [])),
                    }
                    for epoch, window in raw.get("epochs", {}).items()
                }
                current_epoch = int(raw.get("current_epoch", 0))
                if current_epoch not in epochs:
                    epochs[current_epoch] = {"high": -1, "seen": set()}

                fragments = {}
                for fragment in raw.get("fragments", []):
                    msg_seq = _json_restore(fragment["msg_seq"])
                    fragments[msg_seq] = {
                        "total_len": int(fragment["total_len"]),
                        "chunks": [
                            (int(chunk["offset"]), _b64d(chunk["data"]))
                            for chunk in fragment.get("chunks", [])
                        ],
                        "bytes": int(fragment["bytes"]),
                    }

                session = {
                    "session_id": session_id,
                    "five_tuple": five_tuple,
                    "client_random": _json_restore(raw.get("client_random", "")),
                    "cookie": raw.get("cookie"),
                    "state": str(raw.get("state", "handshaking")),
                    "socket_open": bool(raw.get("socket_open", False)),
                    "rtt": float(raw.get("rtt", 0.0)),
                    "early_rejections": int(raw.get("early_rejections", 0)),
                    "current_epoch": current_epoch,
                    "epochs": epochs,
                    "key": _b64d(raw["key"]),
                    "nonce_prefix": _b64d(raw["nonce_prefix"]),
                    "nonce_counter": int(raw.get("nonce_counter", 0)),
                    "fragments": fragments,
                    "fragment_buffered": int(raw.get("fragment_buffered", 0)),
                }
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError("invalid session state") from exc
            sessions[session_id] = session
            tuple_sessions[five_tuple] = session_id

        with self._lock:
            self._cookie_secret = secret
            self._cookies = cookies
            self._sessions = sessions
            self._tuple_sessions = tuple_sessions
        return self.export_state()
