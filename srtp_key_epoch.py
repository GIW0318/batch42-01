"""SRTP key epoch and per-SSRC replay state keeper."""

import base64
import copy
import hashlib
import hmac
import json
import struct
import threading
import time
import uuid


class SrtpError(Exception):
    def __init__(self, code, message=""):
        self.code = code
        super().__init__(code if not message else "%s: %s" % (code, message))


_REPLAY_WINDOW = 256
_SEQ_MASK = 0xFFFF
_SEQ_MOD = 0x10000
_MAX_EXT_SEQ = (1 << 63) - 1
_CHANNELS = ("media", "control")


def _channel(control):
    return "control" if control else "media"


def _as_bytes(value):
    if isinstance(value, (bytes, bytearray, memoryview)):
        return bytes(value)
    if isinstance(value, str):
        return value.encode("utf-8")
    raise SrtpError("EFORMAT", "byte string required")


def _as_int(value):
    if isinstance(value, bool):
        return int(value)
    try:
        return int(value)
    except (TypeError, ValueError):
        raise SrtpError("EFORMAT", "integer required")


def _packet_epoch(packet):
    for name in ("epoch", "key_id", "key_epoch", "keyid"):
        if name in packet and packet[name] is not None:
            return _as_int(packet[name])
    return None


def _estimate_ext_seq(highest, seq):
    """Estimate the extended sequence number from a 16-bit sequence."""
    if highest is None:
        return seq
    highest_low = highest & _SEQ_MASK
    if seq < highest_low and (highest_low - seq) > 0x8000:
        return highest + (seq - highest_low) + _SEQ_MOD
    return highest + (seq - highest_low)


def _candidate_ext_seqs(highest, seq, hinted=None):
    candidates = []

    def add(value):
        if value is None:
            return
        value = int(value)
        if 0 <= value <= _MAX_EXT_SEQ and value not in candidates:
            candidates.append(value)

    add(hinted)
    base = _estimate_ext_seq(highest, seq)
    add(base)
    for delta in (_SEQ_MOD, -_SEQ_MOD, 2 * _SEQ_MOD, -2 * _SEQ_MOD):
        add(base + delta)
    return candidates


def _keystream(key, ssrc, epoch, ext_seq, control, length):
    seed = (
        b"SRTP-ENC"
        + struct.pack(
            ">IIQBB",
            ssrc & 0xFFFFFFFF,
            epoch & 0xFFFFFFFF,
            ext_seq,
            1 if control else 0,
            0,
        )
    )
    output = bytearray()
    counter = 0
    while len(output) < length:
        output.extend(
            hmac.new(key, seed + struct.pack(">I", counter), hashlib.sha256).digest()
        )
        counter += 1
    return bytes(output[:length])


def _crypt(key, ssrc, epoch, ext_seq, control, data):
    stream = _keystream(key, ssrc, epoch, ext_seq, control, len(data))
    return bytes(left ^ right for left, right in zip(data, stream))


def _tag(key, ssrc, epoch, seq, ext_seq, control, retransmit, ciphertext):
    header = b"SRTP-TAG" + struct.pack(
        ">IIQHBB",
        ssrc & 0xFFFFFFFF,
        epoch & 0xFFFFFFFF,
        ext_seq,
        seq,
        1 if control else 0,
        1 if retransmit else 0,
    )
    return hmac.new(key, header + ciphertext, hashlib.sha256).digest()[:16]


class _KeyState:
    __slots__ = ("epoch", "key", "expires_at", "confirmed", "expired", "in_flight")

    def __init__(self, epoch, key, expires_at=None, confirmed=False):
        self.epoch = epoch
        self.key = key
        self.expires_at = expires_at
        self.confirmed = confirmed
        self.expired = False
        # token -> (channel, extended sequence)
        self.in_flight = {}

    def is_expired(self, now):
        return self.expired or (
            self.expires_at is not None and self.expires_at <= now
        )


class _ReplayWindow:
    __slots__ = ("highest", "seen")

    def __init__(self, highest=None, seen=None):
        self.highest = highest
        self.seen = set(seen or ())

    def is_replay(self, ext_seq):
        if self.highest is None:
            return False
        if ext_seq < self.highest - _REPLAY_WINDOW + 1:
            return True
        return ext_seq in self.seen

    def mark(self, ext_seq):
        if self.highest is None or ext_seq > self.highest:
            self.highest = ext_seq
        self.seen.add(ext_seq)
        cutoff = self.highest - _REPLAY_WINDOW + 1
        if cutoff > 0:
            self.seen = {value for value in self.seen if value >= cutoff}


class _StreamState:
    __slots__ = (
        "ssrc",
        "addr",
        "keys",
        "active",
        "pending",
        "replay_count",
        "replay_by_channel",
        "switch_generation",
        "tx",
        "rx",
        "state_lock",
        "packet_lock",
    )

    def __init__(self, ssrc):
        self.ssrc = ssrc
        self.addr = None
        self.keys = {}
        self.active = None
        self.pending = None
        self.replay_count = 0
        self.replay_by_channel = {channel: 0 for channel in _CHANNELS}
        self.switch_generation = 0
        self.tx = {channel: {"highest_ext": None} for channel in _CHANNELS}
        self.rx = {}
        self.state_lock = threading.RLock()
        self.packet_lock = threading.RLock()


class SrtpEpochKeeper:
    def __init__(self):
        self._streams = {}
        self._registry_lock = threading.RLock()
        self._audit_lock = threading.RLock()
        self._audit = []

    # ------------------------------------------------------------------
    # Auditing and stream lookup
    # ------------------------------------------------------------------
    def _record(self, event, **fields):
        entry = {"event": event, "at": time.time()}
        entry.update(fields)
        with self._audit_lock:
            self._audit.append(entry)

    def _require_stream(self, ssrc):
        try:
            ssrc = int(ssrc)
        except (TypeError, ValueError):
            raise SrtpError("EFORMAT", "SSRC must be an integer")
        with self._registry_lock:
            stream = self._streams.get(ssrc)
        if stream is None:
            raise SrtpError("ENOENT", "unknown synchronization source")
        return stream

    # ------------------------------------------------------------------
    # Streams, addresses and source switches
    # ------------------------------------------------------------------
    def add_stream(self, ssrc, *, addr=None):
        ssrc = _as_int(ssrc)
        with self._registry_lock:
            stream = self._streams.get(ssrc)
            if stream is None:
                stream = _StreamState(ssrc)
                self._streams[ssrc] = stream
                created = True
            else:
                created = False
        if addr is not None:
            self.bind_address(ssrc, addr)
        if created:
            self._record("stream_added", ssrc=ssrc, ok=True)
        return created

    def bind_address(self, ssrc, addr):
        stream = self._require_stream(ssrc)
        with stream.packet_lock:
            if stream.addr is None:
                stream.addr = addr
                newly_bound = True
            elif stream.addr == addr:
                newly_bound = False
            else:
                newly_bound = None
        if newly_bound is None:
            self._record(
                "address_bind_failed",
                ssrc=stream.ssrc,
                ok=False,
                reason="EADDRINUSE",
            )
            raise SrtpError("EADDRINUSE", "SSRC already bound to another address")
        if newly_bound:
            self._record("address_bound", ssrc=stream.ssrc, addr=addr, ok=True)
        return newly_bound

    def _check_source_address(self, stream, src_addr):
        if src_addr is None:
            return
        conflict = False
        newly_bound = False
        with stream.packet_lock:
            if stream.addr is None:
                stream.addr = src_addr
                newly_bound = True
            elif stream.addr != src_addr:
                conflict = True
        if conflict:
            self._record(
                "source_address_rejected",
                ssrc=stream.ssrc,
                ok=False,
                reason="EADDR",
            )
            raise SrtpError("EADDR", "packet arrived from an unexpected address")
        if newly_bound:
            self._record("address_bound", ssrc=stream.ssrc, addr=src_addr, ok=True)

    def switch_ssrc(self, ssrc, new_ssrc=None):
        stream = self._require_stream(ssrc)
        with stream.packet_lock:
            stream.addr = None
            stream.rx.clear()
            stream.tx = {channel: {"highest_ext": None} for channel in _CHANNELS}
            stream.switch_generation += 1
        self._record(
            "source_switched",
            ssrc=stream.ssrc,
            new_ssrc=new_ssrc,
            generation=stream.switch_generation,
            ok=True,
        )
        return True

    # ------------------------------------------------------------------
    # Key lifecycle
    # ------------------------------------------------------------------
    def install_key(self, ssrc, epoch, key, *, active=False, expires_at=None):
        stream = self._require_stream(ssrc)
        epoch = _as_int(epoch)
        key_bytes = _as_bytes(key)
        if expires_at is not None:
            expires_at = float(expires_at)
        with stream.state_lock:
            record = stream.keys.get(epoch)
            if record is not None and record.key != key_bytes:
                self._record(
                    "key_install_failed",
                    ssrc=stream.ssrc,
                    epoch=epoch,
                    ok=False,
                    reason="EEXIST",
                )
                raise SrtpError("EEXIST", "key epoch is already installed")
            if record is None:
                record = _KeyState(
                    epoch,
                    key_bytes,
                    expires_at=expires_at,
                    confirmed=bool(active),
                )
                stream.keys[epoch] = record
            else:
                record.expires_at = expires_at
                if active:
                    record.confirmed = True
                record.expired = False
            if active:
                stream.active = epoch
                stream.pending = None
        self._record(
            "key_installed",
            ssrc=stream.ssrc,
            epoch=epoch,
            active=bool(active),
            expiry=expires_at,
            ok=True,
        )
        return True

    def activate(self, ssrc, epoch):
        stream = self._require_stream(ssrc)
        epoch = _as_int(epoch)
        with stream.state_lock:
            record = stream.keys.get(epoch)
            if record is None:
                self._record(
                    "key_activate_failed",
                    ssrc=stream.ssrc,
                    epoch=epoch,
                    ok=False,
                    reason="EKEY",
                )
                raise SrtpError("EKEY", "key epoch is not installed")
            if record.expired:
                self._record(
                    "key_activate_failed",
                    ssrc=stream.ssrc,
                    epoch=epoch,
                    ok=False,
                    reason="EKEYEXPIRED",
                )
                raise SrtpError("EKEYEXPIRED", "key epoch has expired")
            if stream.pending is not None:
                self._record(
                    "key_activate_failed",
                    ssrc=stream.ssrc,
                    epoch=epoch,
                    ok=False,
                    reason="EBUSY",
                    pending_epoch=stream.pending,
                )
                raise SrtpError("EBUSY", "another key switch is still pending")
            if stream.active == epoch:
                self._record(
                    "key_activated",
                    ssrc=stream.ssrc,
                    epoch=epoch,
                    ok=True,
                    already_active=True,
                )
                return True
            stream.active = epoch
            stream.pending = None if record.confirmed else epoch
        self._record("key_activated", ssrc=stream.ssrc, epoch=epoch, ok=True)
        return True

    def confirm(self, ssrc, epoch):
        stream = self._require_stream(ssrc)
        epoch = _as_int(epoch)
        with stream.state_lock:
            record = stream.keys.get(epoch)
            if record is None:
                self._record(
                    "key_confirm_failed",
                    ssrc=stream.ssrc,
                    epoch=epoch,
                    ok=False,
                    reason="EKEY",
                )
                raise SrtpError("EKEY", "key epoch is not installed")
            already_confirmed = record.confirmed
            record.confirmed = True
            if stream.pending == epoch:
                stream.pending = None
        self._record(
            "key_confirmed",
            ssrc=stream.ssrc,
            epoch=epoch,
            ok=True,
            already_confirmed=already_confirmed,
        )
        return not already_confirmed

    def active_epoch(self, ssrc):
        stream = self._require_stream(ssrc)
        with stream.state_lock:
            return stream.active

    def epochs(self, ssrc):
        stream = self._require_stream(ssrc)
        with stream.state_lock:
            return sorted(stream.keys)

    def expire_keys(self, now):
        now = float(now)
        expired_any = []
        with self._registry_lock:
            streams = list(self._streams.values())
        for stream in streams:
            with stream.state_lock:
                expired = []
                for epoch, record in list(stream.keys.items()):
                    if record.expires_at is not None and record.expires_at <= now:
                        if not record.expired:
                            record.expired = True
                            self._record(
                                "key_expired",
                                ssrc=stream.ssrc,
                                epoch=epoch,
                                ok=True,
                            )
                        expired.append(epoch)
                active_record = stream.keys.get(stream.active)
                if active_record is not None and active_record.is_expired(now):
                    candidates = [
                        epoch
                        for epoch, record in stream.keys.items()
                        if epoch != stream.active and not record.is_expired(now)
                    ]
                    if candidates:
                        old_epoch = stream.active
                        replacement = max(candidates)
                        replacement_record = stream.keys[replacement]
                        stream.active = replacement
                        stream.pending = (
                            None if replacement_record.confirmed else replacement
                        )
                        self._record(
                            "key_failover",
                            ssrc=stream.ssrc,
                            from_epoch=old_epoch,
                            to_epoch=replacement,
                            ok=True,
                        )
                expired_any.extend((stream.ssrc, epoch) for epoch in expired)
        return expired_any

    def retire(self, ssrc, epoch):
        stream = self._require_stream(ssrc)
        epoch = _as_int(epoch)
        with stream.state_lock:
            record = stream.keys.get(epoch)
            if record is None:
                self._record(
                    "key_retire_failed",
                    ssrc=stream.ssrc,
                    epoch=epoch,
                    ok=False,
                    reason="EKEY",
                )
                raise SrtpError("EKEY", "key epoch is not installed")
            if stream.active == epoch:
                self._record(
                    "key_retire_failed",
                    ssrc=stream.ssrc,
                    epoch=epoch,
                    ok=False,
                    reason="EACTIVE",
                )
                raise SrtpError("EACTIVE", "cannot retire the active key epoch")
            if record.in_flight:
                self._record(
                    "key_retire_failed",
                    ssrc=stream.ssrc,
                    epoch=epoch,
                    ok=False,
                    reason="EBUSY",
                    in_flight=len(record.in_flight),
                )
                raise SrtpError("EBUSY", "key epoch is still referenced by packets")
            unconfirmed = [
                other_epoch
                for other_epoch, other in stream.keys.items()
                if other_epoch > epoch and not other.confirmed
            ]
            if unconfirmed:
                self._record(
                    "key_retire_failed",
                    ssrc=stream.ssrc,
                    epoch=epoch,
                    ok=False,
                    reason="EUNCONFIRMED",
                    newer_epoch=min(unconfirmed),
                )
                raise SrtpError(
                    "EBUSY", "replacement key has not been confirmed yet"
                )
            del stream.keys[epoch]
            if stream.pending == epoch:
                stream.pending = None
        with stream.packet_lock:
            for key in list(stream.rx):
                if key[1] == epoch:
                    del stream.rx[key]
        self._record("key_retired", ssrc=stream.ssrc, epoch=epoch, ok=True)
        return True

    # ------------------------------------------------------------------
    # Packet protection
    # ------------------------------------------------------------------
    def _select_protect_key_locked(
        self, stream, epoch, *, retransmit=False, now=0.0
    ):
        if epoch is not None:
            epoch = _as_int(epoch)
            record = stream.keys.get(epoch)
            if record is None:
                self._record(
                    "protect_failed",
                    ssrc=stream.ssrc,
                    epoch=epoch,
                    ok=False,
                    reason="EKEY",
                )
                raise SrtpError("EKEY", "key epoch is not available")
            if record.is_expired(now):
                record.expired = True
                self._record(
                    "protect_failed",
                    ssrc=stream.ssrc,
                    epoch=epoch,
                    ok=False,
                    reason="EKEYEXPIRED",
                )
                raise SrtpError("EKEYEXPIRED", "key epoch has expired")
            return record

        if retransmit:
            if stream.active is not None:
                older = [
                    candidate
                    for candidate, record in stream.keys.items()
                    if candidate < stream.active and not record.is_expired(now)
                ]
                selected = max(older) if older else stream.active
            else:
                selected = None
        else:
            selected = stream.active

        record = stream.keys.get(selected)
        if record is None:
            self._record(
                "protect_failed",
                ssrc=stream.ssrc,
                epoch=selected,
                ok=False,
                reason="EKEY",
            )
            raise SrtpError("EKEY", "no active key epoch")

        if record.is_expired(now):
            record.expired = True
            alternatives = [
                candidate
                for candidate, other in stream.keys.items()
                if candidate != selected and not other.is_expired(now)
            ]
            if alternatives and not retransmit:
                replacement = max(alternatives)
                replacement_record = stream.keys[replacement]
                old_epoch = selected
                stream.active = replacement
                stream.pending = (
                    None if replacement_record.confirmed else replacement
                )
                self._record(
                    "key_failover",
                    ssrc=stream.ssrc,
                    from_epoch=old_epoch,
                    to_epoch=replacement,
                    ok=True,
                )
                return replacement_record
            self._record(
                "protect_failed",
                ssrc=stream.ssrc,
                epoch=selected,
                ok=False,
                reason="EKEYEXPIRED",
            )
            raise SrtpError("EKEYEXPIRED", "key epoch has expired")
        return record

    def protect(
        self,
        ssrc,
        seq,
        payload,
        *,
        epoch=None,
        control=False,
        retransmit=False,
        now=0.0,
    ):
        stream = self._require_stream(ssrc)
        payload = _as_bytes(payload)
        channel = _channel(control)
        if seq is not None:
            seq = _as_int(seq) & _SEQ_MASK

        token = uuid.uuid4().hex
        with stream.state_lock:
            record = self._select_protect_key_locked(
                stream, epoch, retransmit=retransmit, now=now
            )
            chosen_epoch = record.epoch
            key = record.key
            record.in_flight[token] = (channel, None)

        try:
            with stream.packet_lock:
                tx = stream.tx[channel]
                if seq is None:
                    if tx["highest_ext"] is None:
                        seq = 0
                        ext_seq = 0
                    else:
                        ext_seq = tx["highest_ext"] + 1
                        seq = ext_seq & _SEQ_MASK
                else:
                    ext_seq = _estimate_ext_seq(tx["highest_ext"], seq)
                if tx["highest_ext"] is None or ext_seq > tx["highest_ext"]:
                    tx["highest_ext"] = ext_seq
                record.in_flight[token] = (channel, ext_seq)

            ciphertext = _crypt(
                key, stream.ssrc, chosen_epoch, ext_seq, control, payload
            )
            tag = _tag(
                key,
                stream.ssrc,
                chosen_epoch,
                seq,
                ext_seq,
                control,
                retransmit,
                ciphertext,
            )
        except Exception:
            with stream.state_lock:
                record.in_flight.pop(token, None)
            raise

        return {
            "ssrc": stream.ssrc,
            "seq": seq,
            "epoch": chosen_epoch,
            "ext_seq": ext_seq,
            "ciphertext": ciphertext,
            "tag": tag,
            "control": bool(control),
            "retransmit": bool(retransmit),
            "_ref": token,
        }

    def _release_in_flight(self, stream, epoch, packet, channel, ext_seq):
        token = packet.get("_ref")
        with stream.state_lock:
            record = stream.keys.get(epoch)
            if record is None:
                return
            if token is not None and token in record.in_flight:
                del record.in_flight[token]
                return
            for ref, value in list(record.in_flight.items()):
                if value == (channel, ext_seq):
                    del record.in_flight[ref]

    def unprotect(self, ssrc, packet, *, src_addr=None):
        stream = self._require_stream(ssrc)
        if not isinstance(packet, dict):
            raise SrtpError("EFORMAT", "packet must be a mapping")

        packet_ssrc = packet.get("ssrc")
        if packet_ssrc is not None and _as_int(packet_ssrc) != stream.ssrc:
            self._record(
                "packet_rejected",
                ssrc=stream.ssrc,
                ok=False,
                reason="ESSRC",
            )
            raise SrtpError("ESSRC", "packet SSRC does not match the stream")

        epoch = _packet_epoch(packet)
        if epoch is None:
            self._record(
                "packet_rejected",
                ssrc=stream.ssrc,
                ok=False,
                reason="EKEY",
            )
            raise SrtpError("EKEY", "packet has no key identifier")

        if packet.get("seq") is None:
            raise SrtpError("EFORMAT", "packet has no sequence number")
        seq = _as_int(packet.get("seq")) & _SEQ_MASK
        if "ciphertext" not in packet or "tag" not in packet:
            raise SrtpError("EFORMAT", "packet is missing ciphertext or tag")
        ciphertext = _as_bytes(packet.get("ciphertext"))
        tag = _as_bytes(packet.get("tag"))
        control = bool(packet.get("control", False))
        retransmit = bool(packet.get("retransmit", False))
        channel = _channel(control)

        self._check_source_address(stream, src_addr)

        with stream.state_lock:
            record = stream.keys.get(epoch)
            if record is None:
                self._record(
                    "packet_rejected",
                    ssrc=stream.ssrc,
                    epoch=epoch,
                    ok=False,
                    reason="EKEY",
                )
                raise SrtpError("EKEY", "key epoch is not available")
            key = record.key

        hinted = packet.get("ext_seq")
        if hinted is not None:
            try:
                hinted = int(hinted)
            except (TypeError, ValueError):
                raise SrtpError("EFORMAT", "invalid extended sequence number")

        accepted_ext = None
        replay_hit = False
        plaintext = None
        with stream.packet_lock:
            rx = stream.rx.setdefault((channel, epoch), _ReplayWindow())
            candidates = _candidate_ext_seqs(rx.highest, seq, hinted)
            for ext_seq in candidates:
                expected_tag = _tag(
                    key,
                    stream.ssrc,
                    epoch,
                    seq,
                    ext_seq,
                    control,
                    retransmit,
                    ciphertext,
                )
                if not hmac.compare_digest(expected_tag, tag):
                    continue
                if rx.is_replay(ext_seq):
                    replay_hit = True
                    break
                rx.mark(ext_seq)
                accepted_ext = ext_seq
                plaintext = _crypt(
                    key, stream.ssrc, epoch, ext_seq, control, ciphertext
                )
                break
            if replay_hit:
                stream.replay_count += 1
                stream.replay_by_channel[channel] += 1
            elif accepted_ext is None:
                self._record(
                    "packet_rejected",
                    ssrc=stream.ssrc,
                    epoch=epoch,
                    seq=seq,
                    ok=False,
                    reason="EAUTH",
                )

        if replay_hit:
            self._record(
                "replay_dropped",
                ssrc=stream.ssrc,
                epoch=epoch,
                seq=seq,
                channel=channel,
                ok=False,
                reason="REPLAY",
            )
            raise SrtpError("REPLAY", "replayed packet")
        if accepted_ext is None:
            raise SrtpError("EAUTH", "packet authentication failed")

        self._release_in_flight(stream, epoch, packet, channel, accepted_ext)
        return plaintext

    def replay_count(self, ssrc, *, control=None):
        stream = self._require_stream(ssrc)
        with stream.packet_lock:
            if control is None:
                return stream.replay_count
            return stream.replay_by_channel[_channel(control)]

    def next_seq(self, ssrc, *, control=False):
        stream = self._require_stream(ssrc)
        channel = _channel(control)
        with stream.packet_lock:
            highest = stream.tx[channel]["highest_ext"]
        if highest is None:
            return 0
        return (highest + 1) & _SEQ_MASK

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------
    def snapshot(self):
        payload = {"version": 1, "streams": {}}
        with self._registry_lock:
            streams = list(self._streams.values())
        for stream in streams:
            with stream.state_lock:
                keys = {}
                for epoch, record in stream.keys.items():
                    keys[str(epoch)] = {
                        "key_b64": base64.b64encode(record.key).decode("ascii"),
                        "expires_at": record.expires_at,
                        "confirmed": bool(record.confirmed),
                        "expired": bool(record.expired),
                        "in_flight": {
                            token: [channel, ext_seq]
                            for token, (channel, ext_seq) in record.in_flight.items()
                        },
                    }
                stream_payload = {
                    "addr": copy.deepcopy(stream.addr),
                    "active": stream.active,
                    "pending": stream.pending,
                    "replay_count": stream.replay_count,
                    "replay_by_channel": copy.deepcopy(
                        stream.replay_by_channel
                    ),
                    "switch_generation": stream.switch_generation,
                    "keys": keys,
                }
                with stream.packet_lock:
                    stream_payload["tx"] = copy.deepcopy(stream.tx)
                    stream_payload["rx"] = {
                        "%s:%d" % (channel, epoch): {
                            "highest": window.highest,
                            "seen": sorted(window.seen),
                        }
                        for (channel, epoch), window in stream.rx.items()
                    }
            payload["streams"][str(stream.ssrc)] = stream_payload
        with self._audit_lock:
            payload["audit_events"] = copy.deepcopy(self._audit)
        return payload

    def restore(self, snapshot):
        if isinstance(snapshot, str):
            try:
                snapshot = json.loads(snapshot)
            except ValueError as exc:
                raise SrtpError("EFORMAT", "invalid snapshot: %s" % exc)
        if not isinstance(snapshot, dict):
            raise SrtpError("EFORMAT", "snapshot must be a mapping")
        raw_streams = snapshot.get("streams")
        if not isinstance(raw_streams, dict):
            raise SrtpError("EFORMAT", "snapshot has no streams")

        streams = {}
        try:
            for raw_ssrc, stream_data in raw_streams.items():
                ssrc = int(raw_ssrc)
                stream = _StreamState(ssrc)
                stream.addr = copy.deepcopy(stream_data.get("addr"))
                active = stream_data.get("active")
                pending = stream_data.get("pending")
                stream.active = None if active is None else int(active)
                stream.pending = None if pending is None else int(pending)
                stream.replay_count = int(stream_data.get("replay_count", 0))
                for channel in _CHANNELS:
                    stream.replay_by_channel[channel] = int(
                        stream_data.get("replay_by_channel", {}).get(channel, 0)
                    )
                stream.switch_generation = int(
                    stream_data.get("switch_generation", 0)
                )
                for raw_epoch, key_data in stream_data.get("keys", {}).items():
                    epoch = int(raw_epoch)
                    raw_key = key_data.get("key_b64")
                    if raw_key is None:
                        raw_key = key_data.get("key")
                    if isinstance(raw_key, str):
                        try:
                            key = base64.b64decode(
                                raw_key.encode("ascii"), validate=True
                            )
                        except Exception:
                            key = raw_key.encode("utf-8")
                    else:
                        key = _as_bytes(raw_key)
                    record = _KeyState(
                        epoch,
                        key,
                        expires_at=(
                            None
                            if key_data.get("expires_at") is None
                            else float(key_data.get("expires_at"))
                        ),
                        confirmed=bool(key_data.get("confirmed", False)),
                    )
                    record.expired = bool(key_data.get("expired", False))
                    for token, value in key_data.get("in_flight", {}).items():
                        if isinstance(value, (list, tuple)) and len(value) == 2:
                            record.in_flight[str(token)] = (
                                value[0],
                                None if value[1] is None else int(value[1]),
                            )
                    stream.keys[epoch] = record
                for channel in _CHANNELS:
                    tx_data = stream_data.get("tx", {}).get(channel, {})
                    highest = tx_data.get("highest_ext")
                    stream.tx[channel]["highest_ext"] = (
                        None if highest is None else int(highest)
                    )
                for raw_key, rx_data in stream_data.get("rx", {}).items():
                    channel, raw_epoch = raw_key.split(":", 1)
                    if channel not in _CHANNELS:
                        continue
                    epoch = int(raw_epoch)
                    highest = rx_data.get("highest")
                    stream.rx[(channel, epoch)] = _ReplayWindow(
                        None if highest is None else int(highest),
                        [int(value) for value in rx_data.get("seen", [])],
                    )
                streams[ssrc] = stream
        except (TypeError, ValueError, KeyError, AttributeError) as exc:
            raise SrtpError("EFORMAT", "invalid snapshot: %s" % exc)

        with self._registry_lock:
            self._streams = streams
        if "audit_events" in snapshot and isinstance(
            snapshot["audit_events"], list
        ):
            with self._audit_lock:
                self._audit = copy.deepcopy(snapshot["audit_events"])
        self._record("state_restored", ok=True, streams=len(streams))
        return True

    def audit_events(self):
        with self._audit_lock:
            return copy.deepcopy(self._audit)

    def __repr__(self):
        return "<SrtpEpochKeeper streams=%d>" % len(self._streams)

    __str__ = __repr__
