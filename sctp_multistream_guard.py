"""SCTP multi-stream, PR-SCTP, path failover and AUTH guard."""

import base64
import collections
import ipaddress
import threading
import time

AUTH_FAILURE_CAUSE = "AUTH_FAILURE"


class SctpError(Exception):
    def __init__(self, code, cause=None, message=""):
        self.code = code
        self.cause = cause
        super().__init__(code if not message else "%s: %s" % (code, message))


class SctpStreamGuard:
    SSN_MODULUS = 1 << 16
    TSN_MODULUS = 1 << 32
    REASSEMBLY_TIMEOUT = 30.0

    def __init__(self, peer_streams=32):
        if peer_streams is None:
            peer_streams = 32
        peer_streams = int(peer_streams)
        if peer_streams < 0:
            raise SctpError("EINVAL", message="peer_streams must be non-negative")
        self.peer_streams = peer_streams
        self.reassembly_timeout = self.REASSEMBLY_TIMEOUT
        self._associations = {}
        self._routes = []
        self._lock = threading.RLock()
        self._clock_origin = time.monotonic()

    def register_association(self, assoc_id, peer_streams=None):
        if peer_streams is None:
            peer_streams = self.peer_streams
        peer_streams = int(peer_streams)
        if peer_streams < 0:
            raise SctpError("EINVAL", message="peer_streams must be non-negative")

        association = {
            "assoc_id": assoc_id,
            "peer_streams": peer_streams,
            "closed": False,
            "next_tsn": 0,
            "streams": {},
            "send_queue": collections.OrderedDict(),
            "paths": collections.OrderedDict(),
            "received_tsns": set(),
            "tsn_owners": {},
            "duplicate_total": 0,
            "duplicate_by_stream": {},
            "reassembly": {},
        }
        with self._lock:
            self._associations[assoc_id] = association
        return assoc_id

    def send(self, assoc_id, stream_id, payload):
        with self._lock:
            association = self._require_association(assoc_id)
            stream_id = self._validate_stream(association, stream_id)
            stream = self._stream_state(association, stream_id)

            if stream["rwnd"] is not None and stream["rwnd"] <= 0:
                raise SctpError("EWOULDBLOCK", message="receive window is zero")

            path_id = None
            if association["paths"]:
                path_id = self._active_path_locked(association)
                if path_id is None:
                    raise SctpError("EHOSTUNREACH", message="no reachable path")

            data = self._payload_bytes(payload)
            ssn = stream["next_ssn"]
            stream["next_ssn"] = (ssn + 1) % self.SSN_MODULUS

            tsn = association["next_tsn"]
            association["next_tsn"] = (tsn + 1) % self.TSN_MODULUS

            message = {
                "tsn": tsn,
                "ssn": ssn,
                "stream_id": stream_id,
                "payload": data,
                "bytes": len(data),
                "path_id": path_id,
            }
            association["send_queue"][tsn] = message
            result = dict(message)
            result["stream"] = stream_id
            result["size"] = result["bytes"]
            result["assoc_id"] = assoc_id
            return result

    def abandon(self, tsn):
        tsn = int(tsn)
        with self._lock:
            for association in self._associations.values():
                if tsn in association["send_queue"]:
                    association["send_queue"].pop(tsn)
                    return True
        return False

    def pending(self, assoc_id):
        with self._lock:
            association = self._require_association(assoc_id, allow_closed=True)
            return {
                "bytes": sum(item["bytes"] for item in association["send_queue"].values()),
                "messages": len(association["send_queue"]),
            }

    def reassembly_bytes(self, assoc_id, stream_id=None):
        with self._lock:
            association = self._require_association(assoc_id, allow_closed=True)
            total = 0
            for (buffered_stream, _), buffer in association["reassembly"].items():
                if stream_id is None or buffered_stream == stream_id:
                    total += buffer["bytes"]
            return total

    def receive_chunk(self, assoc_id, stream_id, tsn, ssn, data, *, last=False):
        with self._lock:
            association = self._require_association(assoc_id)
            stream_id = self._validate_stream(association, stream_id)
            self._stream_state(association, stream_id)

            self._sweep_reassembly_locked(association, self._now())

            tsn = int(tsn) % self.TSN_MODULUS
            ssn = int(ssn) % self.SSN_MODULUS
            if tsn in association["received_tsns"]:
                owner = association["tsn_owners"].get(tsn, stream_id)
                association["duplicate_total"] += 1
                by_stream = association["duplicate_by_stream"]
                by_stream[owner] = by_stream.get(owner, 0) + 1
                return {
                    "duplicate": True,
                    "complete": False,
                    "payload": b"",
                    "tsn": tsn,
                    "ssn": ssn,
                    "stream_id": stream_id,
                }

            association["received_tsns"].add(tsn)
            association["tsn_owners"][tsn] = stream_id
            chunk = self._payload_bytes(data)
            key = (stream_id, ssn)
            buffer = association["reassembly"].get(key)
            if buffer is None:
                buffer = {
                    "created": self._now(),
                    "bytes": 0,
                    "pieces": {},
                }
                association["reassembly"][key] = buffer

            if tsn not in buffer["pieces"]:
                buffer["pieces"][tsn] = (chunk, bool(last))
                buffer["bytes"] += len(chunk)

            if not last:
                return {
                    "duplicate": False,
                    "complete": False,
                    "payload": b"",
                    "tsn": tsn,
                    "ssn": ssn,
                    "stream_id": stream_id,
                }

            payload = b"".join(
                piece for _, (piece, _) in sorted(buffer["pieces"].items())
            )
            association["reassembly"].pop(key, None)
            return {
                "duplicate": False,
                "complete": True,
                "payload": payload,
                "tsn": tsn,
                "ssn": ssn,
                "stream_id": stream_id,
            }

    def duplicate_count(self, assoc_id, stream_id=None):
        with self._lock:
            association = self._require_association(assoc_id, allow_closed=True)
            if stream_id is None:
                return association["duplicate_total"]
            return association["duplicate_by_stream"].get(int(stream_id), 0)

    def sweep_reassembly(self, now=None):
        with self._lock:
            if now is None:
                now = self._now()
            else:
                now = float(now)
            freed = 0
            for association in self._associations.values():
                freed += self._sweep_reassembly_locked(association, now)
            return freed

    def set_rwnd(self, assoc_id, stream_id, rwnd):
        with self._lock:
            association = self._require_association(assoc_id)
            stream_id = self._validate_stream(association, stream_id)
            rwnd = int(rwnd)
            if rwnd < 0:
                raise SctpError("EINVAL", message="rwnd must be non-negative")
            stream = self._stream_state(association, stream_id)
            stream["rwnd"] = rwnd
            return {"stream_id": stream_id, "rwnd": rwnd}

    def add_path(self, assoc_id, path_id, primary=False):
        with self._lock:
            association = self._require_association(assoc_id)
            paths = association["paths"]
            if path_id in paths:
                paths[path_id]["primary"] = bool(primary)
            else:
                paths[path_id] = {
                    "primary": bool(primary),
                    "reachable": True,
                    "cumack_tsn": None,
                    "acked_tsns": set(),
                }
            if primary:
                for other_id, other in paths.items():
                    if other_id != path_id:
                        other["primary"] = False
            return self._path_result(path_id, paths[path_id])

    def set_path_state(self, assoc_id, path_id, reachable):
        with self._lock:
            association = self._require_association(assoc_id)
            path = association["paths"].get(path_id)
            if path is None:
                raise SctpError("EINVAL", message="unknown path")
            path["reachable"] = bool(reachable)
            return self._path_result(path_id, path)

    def heartbeat_timeout(self, assoc_id, path_id):
        with self._lock:
            association = self._require_association(assoc_id)
            path = association["paths"].get(path_id)
            if path is None:
                raise SctpError("EINVAL", message="unknown path")
            path["reachable"] = False
            association_alive = any(
                candidate["reachable"] for candidate in association["paths"].values()
            )
            return {
                "association_alive": association_alive,
                "path_id": path_id,
                "path": path_id,
                "reachable": False,
                "active_path": self._active_path_locked(association),
            }

    def path_state(self, assoc_id, path_id):
        with self._lock:
            association = self._require_association(assoc_id, allow_closed=True)
            path = association["paths"].get(path_id)
            return bool(path and path["reachable"])

    def active_path(self, assoc_id):
        with self._lock:
            association = self._require_association(assoc_id, allow_closed=True)
            return self._active_path_locked(association)

    def cumack(self, assoc_id, tsn, path_id):
        with self._lock:
            association = self._require_association(assoc_id)
            path = association["paths"].get(path_id)
            if path is None:
                raise SctpError("EINVAL", message="unknown path")

            tsn = int(tsn) % self.TSN_MODULUS
            for queued_tsn, message in association["send_queue"].items():
                if message.get("path_id") == path_id:
                    path["acked_tsns"].add(queued_tsn)
            if path["cumack_tsn"] is None or self._tsn_lt(path["cumack_tsn"], tsn):
                path["cumack_tsn"] = tsn
            self._prune_acked_locked(association)
            return path["cumack_tsn"]

    def unacked_tsns(self, assoc_id, path_id=None):
        with self._lock:
            association = self._require_association(assoc_id, allow_closed=True)
            if path_id is None:
                return list(association["send_queue"].keys())
            path = association["paths"].get(path_id)
            if path is None:
                raise SctpError("EINVAL", message="unknown path")
            return [
                tsn
                for tsn in association["send_queue"]
                if tsn not in path["acked_tsns"]
            ]

    def authenticate(self, assoc_id, chunk, *, valid=True):
        with self._lock:
            association = self._require_association(assoc_id)
            if not valid:
                association["closed"] = True
                association["send_queue"].clear()
                association["reassembly"].clear()
                raise SctpError(
                    "ABORT",
                    AUTH_FAILURE_CAUSE,
                    "AUTH chunk validation failed",
                )
            return {
                "authenticated": True,
                "assoc_id": assoc_id,
                "association_alive": not association["closed"],
            }

    def association_closed(self, assoc_id):
        with self._lock:
            association = self._associations.get(assoc_id)
            return bool(association and association["closed"])

    def add_route(self, prefix, source):
        try:
            network = ipaddress.ip_network(prefix, strict=False)
            ipaddress.ip_address(source)
        except ValueError as exc:
            raise SctpError("EINVAL", message=str(exc)) from exc
        with self._lock:
            self._routes.append((network, str(source)))
        return None

    def select_source(self, assoc_id, dest):
        with self._lock:
            self._require_association(assoc_id, allow_closed=True)
            try:
                destination = ipaddress.ip_address(dest)
            except ValueError as exc:
                raise SctpError("EINVAL", message=str(exc)) from exc

            best = None
            for network, source in self._routes:
                if network.version != destination.version:
                    continue
                if destination in network:
                    if best is None or network.prefixlen >= best[0]:
                        best = (network.prefixlen, source)
            return None if best is None else best[1]

    def next_ssn(self, assoc_id, stream_id):
        with self._lock:
            association = self._require_association(assoc_id, allow_closed=True)
            stream_id = self._validate_stream(association, stream_id)
            return self._stream_state(association, stream_id)["next_ssn"]

    def snapshot(self, assoc_id):
        with self._lock:
            association = self._require_association(assoc_id, allow_closed=True)
            streams = {}
            for stream_id, stream in association["streams"].items():
                streams[str(stream_id)] = {
                    "next_ssn": stream["next_ssn"],
                    "rwnd": stream["rwnd"],
                }

            paths = {}
            for path_id, path in association["paths"].items():
                paths[str(path_id)] = {
                    "primary": path["primary"],
                    "reachable": path["reachable"],
                    "cumack_tsn": path["cumack_tsn"],
                    "acked_tsns": sorted(path["acked_tsns"]),
                }

            send_queue = []
            for message in association["send_queue"].values():
                send_queue.append(
                    {
                        "tsn": message["tsn"],
                        "ssn": message["ssn"],
                        "stream_id": message["stream_id"],
                        "payload": self._encode_bytes(message["payload"]),
                        "payload_encoding": "base64",
                        "bytes": message["bytes"],
                        "path_id": message["path_id"],
                    }
                )

            reassembly = []
            for (stream_id, ssn), buffer in association["reassembly"].items():
                pieces = []
                for piece_tsn, (piece_data, piece_last) in sorted(
                    buffer["pieces"].items()
                ):
                    pieces.append(
                        {
                            "tsn": piece_tsn,
                            "data": self._encode_bytes(piece_data),
                            "last": piece_last,
                        }
                    )
                reassembly.append(
                    {
                        "stream_id": stream_id,
                        "ssn": ssn,
                        "created": buffer["created"],
                        "bytes": buffer["bytes"],
                        "pieces": pieces,
                    }
                )

            return {
                "version": 1,
                "assoc_id": assoc_id,
                "peer_streams": association["peer_streams"],
                "closed": association["closed"],
                "next_tsn": association["next_tsn"],
                "streams": streams,
                "send_queue": send_queue,
                "unacked_tsns": list(association["send_queue"].keys()),
                "paths": paths,
                "received_tsns": sorted(association["received_tsns"]),
                "tsn_owners": {
                    str(tsn): stream_id
                    for tsn, stream_id in association["tsn_owners"].items()
                },
                "duplicate_total": association["duplicate_total"],
                "duplicate_by_stream": {
                    str(stream_id): count
                    for stream_id, count in association["duplicate_by_stream"].items()
                },
                "reassembly": reassembly,
            }

    def restore(self, assoc_id, snapshot):
        peer_streams = int(snapshot.get("peer_streams", self.peer_streams))
        association = {
            "assoc_id": assoc_id,
            "peer_streams": peer_streams,
            "closed": bool(snapshot.get("closed", False)),
            "next_tsn": int(snapshot.get("next_tsn", 0)) % self.TSN_MODULUS,
            "streams": {},
            "send_queue": collections.OrderedDict(),
            "paths": collections.OrderedDict(),
            "received_tsns": set(),
            "tsn_owners": {},
            "duplicate_total": int(snapshot.get("duplicate_total", 0)),
            "duplicate_by_stream": {},
            "reassembly": {},
        }

        for stream_key, stream_snapshot in snapshot.get("streams", {}).items():
            stream_id = int(stream_key)
            association["streams"][stream_id] = {
                "next_ssn": int(stream_snapshot.get("next_ssn", 0))
                % self.SSN_MODULUS,
                "rwnd": stream_snapshot.get("rwnd"),
            }

        queue_entries = snapshot.get("send_queue", [])
        if not queue_entries:
            queue_entries = [{"tsn": tsn} for tsn in snapshot.get("unacked_tsns", [])]
        for entry in queue_entries:
            tsn = int(entry["tsn"]) % self.TSN_MODULUS
            stream_id = int(entry.get("stream_id", 0))
            payload = self._decode_bytes(entry.get("payload", b""))
            message = {
                "tsn": tsn,
                "ssn": int(entry.get("ssn", 0)) % self.SSN_MODULUS,
                "stream_id": stream_id,
                "payload": payload,
                "bytes": int(entry.get("bytes", len(payload))),
                "path_id": entry.get("path_id"),
            }
            association["send_queue"][tsn] = message
            if stream_id not in association["streams"]:
                association["streams"][stream_id] = {
                    "next_ssn": (message["ssn"] + 1) % self.SSN_MODULUS,
                    "rwnd": None,
                }

        for path_key, path_snapshot in snapshot.get("paths", {}).items():
            path_id = self._restore_key(path_key)
            association["paths"][path_id] = {
                "primary": bool(path_snapshot.get("primary", False)),
                "reachable": bool(path_snapshot.get("reachable", True)),
                "cumack_tsn": path_snapshot.get("cumack_tsn"),
                "acked_tsns": set(
                    int(tsn) % self.TSN_MODULUS
                    for tsn in path_snapshot.get("acked_tsns", [])
                ),
            }

        for tsn in snapshot.get("received_tsns", []):
            association["received_tsns"].add(int(tsn) % self.TSN_MODULUS)
        for tsn_key, stream_id in snapshot.get("tsn_owners", {}).items():
            association["tsn_owners"][int(tsn_key) % self.TSN_MODULUS] = int(stream_id)
        for stream_key, count in snapshot.get("duplicate_by_stream", {}).items():
            association["duplicate_by_stream"][int(stream_key)] = int(count)

        for buffer_snapshot in snapshot.get("reassembly", []):
            stream_id = int(buffer_snapshot["stream_id"])
            ssn = int(buffer_snapshot["ssn"]) % self.SSN_MODULUS
            created = float(buffer_snapshot.get("created", 0.0))
            created = min(created, self._now())
            buffer = {
                "created": created,
                "bytes": int(buffer_snapshot.get("bytes", 0)),
                "pieces": {},
            }
            for piece in buffer_snapshot.get("pieces", []):
                piece_tsn = int(piece["tsn"]) % self.TSN_MODULUS
                buffer["pieces"][piece_tsn] = (
                    self._decode_bytes(piece.get("data", b"")),
                    bool(piece.get("last", False)),
                )
            association["reassembly"][(stream_id, ssn)] = buffer

        with self._lock:
            self._associations[assoc_id] = association
        return assoc_id

    def _require_association(self, assoc_id, allow_closed=False):
        association = self._associations.get(assoc_id)
        if association is None:
            raise SctpError("ENOTCONN", message="unknown association")
        if association["closed"] and not allow_closed:
            raise SctpError("ENOTCONN", message="association is closed")
        return association

    def _validate_stream(self, association, stream_id):
        try:
            stream_id = int(stream_id)
        except (TypeError, ValueError) as exc:
            raise SctpError("EINVAL", message="invalid stream id") from exc
        if stream_id < 0 or stream_id >= association["peer_streams"]:
            raise SctpError(
                "EINVAL",
                message="stream id is outside the negotiated range",
            )
        return stream_id

    def _stream_state(self, association, stream_id):
        stream = association["streams"].get(stream_id)
        if stream is None:
            stream = {"next_ssn": 0, "rwnd": None}
            association["streams"][stream_id] = stream
        return stream

    def _active_path_locked(self, association):
        paths = association["paths"]
        for path_id, path in paths.items():
            if path["primary"] and path["reachable"]:
                return path_id
        for path_id, path in paths.items():
            if path["reachable"]:
                return path_id
        return None

    def _path_result(self, path_id, path):
        return {
            "path_id": path_id,
            "path": path_id,
            "primary": path["primary"],
            "reachable": path["reachable"],
        }

    def _prune_acked_locked(self, association):
        paths = association["paths"]
        if not paths:
            return
        acked_everywhere = [
            tsn
            for tsn in association["send_queue"]
            if all(tsn in path["acked_tsns"] for path in paths.values())
        ]
        for tsn in acked_everywhere:
            association["send_queue"].pop(tsn, None)

    def _sweep_reassembly_locked(self, association, now):
        timeout = float(getattr(self, "reassembly_timeout", self.REASSEMBLY_TIMEOUT))
        expired = []
        for key, buffer in association["reassembly"].items():
            if now - buffer["created"] >= timeout:
                expired.append(key)
        freed = 0
        for key in expired:
            freed += association["reassembly"][key]["bytes"]
            association["reassembly"].pop(key, None)
        return freed

    def _now(self):
        return time.monotonic() - self._clock_origin

    def _tsn_le(self, left, right):
        return ((right - left) % self.TSN_MODULUS) < (self.TSN_MODULUS // 2)

    def _tsn_lt(self, left, right):
        return left != right and self._tsn_le(left, right)

    @staticmethod
    def _payload_bytes(payload):
        if isinstance(payload, bytes):
            return payload
        if isinstance(payload, (bytearray, memoryview)):
            return bytes(payload)
        if isinstance(payload, str):
            return payload.encode("utf-8")
        try:
            return bytes(payload)
        except (TypeError, ValueError) as exc:
            raise SctpError("EINVAL", message="payload must be bytes-like") from exc

    @staticmethod
    def _encode_bytes(value):
        return base64.b64encode(bytes(value)).decode("ascii")

    @staticmethod
    def _decode_bytes(value):
        if isinstance(value, bytes):
            return value
        if isinstance(value, bytearray):
            return bytes(value)
        if isinstance(value, str):
            try:
                return base64.b64decode(value.encode("ascii"), validate=True)
            except (ValueError, UnicodeEncodeError):
                return value.encode("utf-8")
        if isinstance(value, list):
            return bytes(value)
        return bytes(value)

    @staticmethod
    def _restore_key(value):
        try:
            return int(value)
        except (TypeError, ValueError):
            return value
