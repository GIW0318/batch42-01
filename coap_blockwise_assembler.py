"""CoAP Block1 reassembly.

Uploads are keyed by ``(token, resource)``.  Each upload keeps the block size
and ETag fixed by its first block, accepts only the next contiguous block, and
tracks the total body size it has buffered.
"""

from __future__ import annotations

import base64
import json
import threading
from dataclasses import dataclass, field
from typing import Any, Dict, Hashable, Optional, Tuple


_MAX_BLOCK_NUMBER = 0xFFFF
_STATE_VERSION = 1
_BYTES_TAG = "$coap_bytes"
_TUPLE_TAG = "$coap_tuple"


class BlockNumberOutOfRange(Exception):
    """Raised when a Block1 number does not fit the 16 bit option."""


@dataclass
class _Upload:
    block_size: int
    etag: Any
    expected_block: int
    last_seen: float
    body: bytearray = field(default_factory=bytearray)


class CoapBlockAssembler:
    def __init__(
        self, max_total_bytes=8 * 1024 * 1024, max_block_size=1024, timeout=30.0
    ):
        if max_total_bytes is not None and max_total_bytes < 0:
            raise ValueError("max_total_bytes must be non-negative")
        if max_block_size is not None and max_block_size <= 0:
            raise ValueError("max_block_size must be positive")
        if timeout is not None and timeout < 0:
            raise ValueError("timeout must be non-negative")

        self.max_total_bytes = max_total_bytes
        self.max_block_size = max_block_size
        self.timeout = timeout
        self._uploads: Dict[Tuple[Hashable, Hashable], _Upload] = {}
        self._lock = threading.RLock()

    def put_block(
        self,
        token,
        resource,
        block_num,
        more,
        payload,
        block_size=1024,
        etag=None,
        now=0.0,
    ):
        if (
            not isinstance(block_num, int)
            or isinstance(block_num, bool)
            or block_num < 0
            or block_num > _MAX_BLOCK_NUMBER
        ):
            raise BlockNumberOutOfRange(block_num)

        data = bytes(payload)
        key = (token, resource)

        with self._lock:
            upload = self._uploads.get(key)

            if upload is None:
                return self._start_upload(
                    key, block_num, more, data, block_size, etag, now
                )

            if block_num == 0 and etag != upload.etag:
                return self._failure(
                    "4.09 Conflict",
                    expected_block=upload.expected_block,
                    reason="token conflict",
                )

            if (
                self.max_block_size is not None
                and block_size > self.max_block_size
            ):
                self._uploads.pop(key, None)
                return self._failure(
                    "4.13 Request Entity Too Large",
                    expected_block=upload.expected_block,
                    reason="block size too large",
                    szx=self.max_block_size,
                )

            if block_size != upload.block_size:
                self._uploads.pop(key, None)
                return self._failure(
                    "4.08 Request Entity Incomplete",
                    expected_block=upload.expected_block,
                    reason="block size changed",
                )

            if etag != upload.etag:
                self._uploads.pop(key, None)
                return self._failure(
                    "4.08 Request Entity Incomplete",
                    expected_block=upload.expected_block,
                    reason="etag changed",
                )

            if block_num < upload.expected_block:
                upload.last_seen = now
                return self._continue(upload, duplicate=True)

            if block_num > upload.expected_block:
                return self._failure(
                    "4.08 Request Entity Incomplete",
                    expected_block=upload.expected_block,
                )

            if (
                self.max_total_bytes is not None
                and len(upload.body) + len(data) > self.max_total_bytes
            ):
                self._uploads.pop(key, None)
                return self._failure(
                    "4.13 Request Entity Too Large",
                    expected_block=upload.expected_block,
                    reason="entity too large",
                )

            upload.body.extend(data)
            upload.last_seen = now

            if more:
                upload.expected_block += 1
                return self._continue(upload)

            body = bytes(upload.body)
            self._uploads.pop(key, None)
            return self._complete(body)

    def _start_upload(
        self, key, block_num, more, data, block_size, etag, now
    ):
        if (
            self.max_block_size is not None
            and block_size > self.max_block_size
        ):
            return self._failure(
                "4.13 Request Entity Too Large",
                expected_block=0,
                reason="block size too large",
                szx=self.max_block_size,
            )

        if block_num != 0:
            return self._failure(
                "4.08 Request Entity Incomplete",
                expected_block=0,
            )

        if (
            self.max_total_bytes is not None
            and len(data) > self.max_total_bytes
        ):
            return self._failure(
                "4.13 Request Entity Too Large",
                expected_block=0,
                reason="entity too large",
            )

        if not more:
            return self._complete(data)

        self._uploads[key] = _Upload(
            block_size=block_size,
            etag=etag,
            expected_block=1,
            last_seen=now,
            body=bytearray(data),
        )
        return self._continue(self._uploads[key])

    @staticmethod
    def _continue(upload: _Upload, duplicate=False):
        return {
            "code": "2.31 Continue",
            "complete": False,
            "duplicate": duplicate,
            "expected_block": upload.expected_block,
        }

    @staticmethod
    def _complete(body: bytes):
        return {
            "code": "2.04 Changed",
            "complete": True,
            "duplicate": False,
            "expected_block": None,
            "body": body,
        }

    @staticmethod
    def _failure(
        code,
        *,
        expected_block: Optional[int],
        reason: Optional[str] = None,
        szx: Optional[int] = None,
    ):
        result = {
            "code": code,
            "complete": False,
            "duplicate": False,
            "expected_block": expected_block,
        }
        if reason is not None:
            result["reason"] = reason
        if szx is not None:
            result["szx"] = szx
        return result

    def pending_uploads(self):
        with self._lock:
            return list(self._uploads.keys())

    def buffered_bytes(self):
        with self._lock:
            return sum(len(upload.body) for upload in self._uploads.values())

    def expire(self, now):
        with self._lock:
            expired = []
            for key, upload in self._uploads.items():
                if (
                    self.timeout is not None
                    and now - upload.last_seen > self.timeout
                ):
                    expired.append(key)

            freed = sum(len(self._uploads[key].body) for key in expired)
            for key in expired:
                del self._uploads[key]

            return {"expired": len(expired), "freed_bytes": freed}

    def export_state(self):
        with self._lock:
            uploads = []
            for (token, resource), upload in self._uploads.items():
                uploads.append(
                    {
                        "token": _encode_value(token),
                        "resource": _encode_value(resource),
                        "block_size": upload.block_size,
                        "etag": _encode_value(upload.etag),
                        "expected_block": upload.expected_block,
                        "last_seen": upload.last_seen,
                        "body": _encode_value(bytes(upload.body)),
                    }
                )
            return {"version": _STATE_VERSION, "uploads": uploads}

    def load_state(self, state):
        if isinstance(state, (str, bytes, bytearray)):
            try:
                state = json.loads(state)
            except (TypeError, ValueError) as exc:
                raise ValueError("invalid assembler state") from exc

        if not isinstance(state, dict):
            raise ValueError("invalid assembler state")
        if state.get("version") != _STATE_VERSION:
            raise ValueError("unsupported assembler state version")

        raw_uploads = state.get("uploads")
        if not isinstance(raw_uploads, list):
            raise ValueError("invalid assembler state")

        restored: Dict[Tuple[Hashable, Hashable], _Upload] = {}
        for item in raw_uploads:
            if not isinstance(item, dict):
                raise ValueError("invalid upload state")

            try:
                token = _decode_value(item["token"])
                resource = _decode_value(item["resource"])
                etag = _decode_value(item["etag"])
                body_value = _decode_value(item["body"])
                block_size = item["block_size"]
                expected_block = item["expected_block"]
                last_seen = item["last_seen"]
                key = (token, resource)
                hash(key)
            except (KeyError, TypeError) as exc:
                raise ValueError("invalid upload state") from exc

            if not isinstance(body_value, bytes):
                raise ValueError("invalid upload body")
            if (
                not isinstance(block_size, int)
                or isinstance(block_size, bool)
                or block_size <= 0
                or (
                    self.max_block_size is not None
                    and block_size > self.max_block_size
                )
            ):
                raise ValueError("invalid upload block size")
            if (
                not isinstance(expected_block, int)
                or isinstance(expected_block, bool)
                or expected_block < 1
                or expected_block > _MAX_BLOCK_NUMBER + 1
            ):
                raise ValueError("invalid upload block number")
            if not isinstance(last_seen, (int, float)) or isinstance(
                last_seen, bool
            ):
                raise ValueError("invalid upload timestamp")
            if (
                self.max_total_bytes is not None
                and len(body_value) > self.max_total_bytes
            ):
                raise ValueError("upload exceeds maximum size")
            if key in restored:
                raise ValueError("duplicate upload state")

            restored[key] = _Upload(
                block_size=block_size,
                etag=etag,
                expected_block=expected_block,
                last_seen=float(last_seen),
                body=bytearray(body_value),
            )

        with self._lock:
            self._uploads = restored


def _encode_value(value):
    if isinstance(value, bytes):
        return {
            _BYTES_TAG: base64.b64encode(value).decode("ascii"),
        }
    if isinstance(value, bytearray):
        return _encode_value(bytes(value))
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, tuple):
        return {_TUPLE_TAG: [_encode_value(item) for item in value]}
    raise TypeError(f"cannot persist value of type {type(value).__name__}")


def _decode_value(value):
    if isinstance(value, dict):
        if set(value) == {_BYTES_TAG}:
            try:
                return base64.b64decode(value[_BYTES_TAG], validate=True)
            except (TypeError, ValueError) as exc:
                raise ValueError("invalid encoded bytes") from exc
        if set(value) == {_TUPLE_TAG}:
            items = value[_TUPLE_TAG]
            if not isinstance(items, list):
                raise ValueError("invalid encoded tuple")
            return tuple(_decode_value(item) for item in items)
    return value
