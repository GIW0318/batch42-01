"""Multitrack stem rendering, latency compensation and export.

Stems sum to the master within tolerance, mute and solo are frozen at export
start, plugin delay is compensated per sample and export is atomic.
"""

import os
import struct

EXPORT_FAILED = "export_failed"
UNKNOWN_TRACK = "unknown_track"
UNKNOWN_EXPORT = "unknown_export"


class MixdownError(Exception):
    def __init__(self, code, message=""):
        self.code = code
        super().__init__(code if not message else "%s: %s" % (code, message))


class _Track:
    __slots__ = ("track_id", "name", "samples", "gain_db", "muted", "solo",
                 "delay", "automation", "clips")

    def __init__(self, track_id, samples=None, name=None):
        self.track_id = track_id
        self.name = name
        self.samples = list(samples) if samples else []
        self.gain_db = 0.0
        self.muted = False
        self.solo = False
        self.delay = 0
        self.automation = []
        self.clips = []


class MultitrackRenderMixdown:
    def __init__(self, sample_rate=48000, block_size=512):
        self.sample_rate = sample_rate
        self.block_size = block_size
        self._tracks = {}
        self._limiter = False
        self._exported = False
        self._tx_counter = 0
        self._transactions = {}

    def _track(self, track_id):
        try:
            return self._tracks[track_id]
        except KeyError:
            raise MixdownError(UNKNOWN_TRACK, track_id)

    def _transaction(self, tx):
        try:
            return self._transactions[tx]
        except KeyError:
            raise MixdownError(UNKNOWN_EXPORT, str(tx))

    def add_track(self, track_id, samples=None, name=None):
        self._tracks[track_id] = _Track(track_id, samples, name)
        return track_id

    def set_track_gain(self, track_id, db):
        self._track(track_id).gain_db = float(db)

    def set_mute(self, track_id, muted):
        self._track(track_id).muted = bool(muted)

    def set_solo(self, track_id, solo):
        self._track(track_id).solo = bool(solo)

    def set_plugin_delay(self, track_id, samples):
        self._track(track_id).delay = int(samples)

    def set_automation(self, track_id, points):
        track = self._track(track_id)
        track.automation = sorted((int(t), float(db)) for t, db in points)

    def add_clip(self, track_id, start, length, fade_in=0, fade_out=0):
        track = self._track(track_id)
        track.clips.append({
            "start": int(start),
            "length": int(length),
            "fade_in": int(fade_in),
            "fade_out": int(fade_out),
        })

    def _track_length(self, track):
        length = len(track.samples)
        for clip in track.clips:
            length = max(length, clip["start"] + clip["length"])
        return length

    def _snapshot_state(self):
        return {
            track_id: {
                "gain_db": track.gain_db,
                "muted": track.muted,
                "solo": track.solo,
            }
            for track_id, track in self._tracks.items()
        }

    def _audible_ids(self, state=None):
        if state is None:
            state = self._snapshot_state()
        any_solo = any(entry["solo"] for entry in state.values())
        return [
            track_id for track_id in self._tracks
            if not state[track_id]["muted"]
            and (not any_solo or state[track_id]["solo"])
        ]

    @staticmethod
    def _envelope_gain(envelope, index):
        sup_start, sup_end, up_len, down_len = envelope
        if index < sup_start or index >= sup_end:
            return 0.0
        if up_len > 0:
            up = (index - sup_start) / float(up_len)
        else:
            up = 1.0
        if down_len > 0:
            down = (sup_end - index) / float(down_len)
        else:
            down = 1.0
        return max(0.0, min(1.0, up, down))

    def _clip_envelopes(self, track):
        clips = sorted(track.clips, key=lambda clip: clip["start"])
        envelopes = []
        for clip in clips:
            start = clip["start"]
            end = start + clip["length"]
            left = None
            right = None
            for other in clips:
                if other is clip:
                    continue
                if other["start"] + other["length"] == start:
                    left = other
                if other["start"] == end:
                    right = other
            sup_start = start - (left["fade_out"] if left else 0)
            sup_end = end + (right["fade_in"] if right else 0)
            up_len = (left["fade_out"] if left else 0) + clip["fade_in"]
            down_len = clip["fade_out"] + (right["fade_in"] if right else 0)
            envelopes.append((sup_start, sup_end, up_len, down_len))
        return envelopes

    def _automation_gain(self, track, index):
        points = track.automation
        if not points:
            return 1.0
        if index <= points[0][0]:
            db = points[0][1]
        elif index >= points[-1][0]:
            db = points[-1][1]
        else:
            db = points[-1][1]
            for (t0, db0), (t1, db1) in zip(points, points[1:]):
                if t0 <= index <= t1:
                    span = t1 - t0
                    ratio = 0.0 if span == 0 else (index - t0) / float(span)
                    db = db0 + (db1 - db0) * ratio
                    break
        return 10.0 ** (db / 20.0)

    def _render_track_samples(self, track, gain_db):
        length = self._track_length(track)
        envelopes = self._clip_envelopes(track)
        track_gain = 10.0 ** (gain_db / 20.0)
        out = []
        for index in range(length):
            value = track.samples[index] if index < len(track.samples) else 0.0
            for envelope in envelopes:
                value += self._envelope_gain(envelope, index)
            value *= track_gain * self._automation_gain(track, index)
            out.append(value)
        delay = track.delay
        if delay > 0:
            delayed = [0.0] * delay + out
            out = delayed[delay:delay + length]
        return out

    def render_stem(self, track_id):
        track = self._track(track_id)
        if track_id not in self._audible_ids():
            return {"track": track_id, "rendered": False, "samples": []}
        return {
            "track": track_id,
            "rendered": True,
            "samples": self._render_track_samples(track, track.gain_db),
        }

    def render_master(self):
        length = 0
        for track in self._tracks.values():
            length = max(length, self._track_length(track))
        master = [0.0] * length
        for track_id in self._audible_ids():
            track = self._tracks[track_id]
            stem = self._render_track_samples(track, track.gain_db)
            for index, value in enumerate(stem):
                master[index] += value
        return {"samples": master}

    def begin_export(self):
        self._tx_counter += 1
        tx = "tx-%d" % self._tx_counter
        self._transactions[tx] = {
            "state": self._snapshot_state(),
            "completed": [],
            "committed": False,
        }
        return tx

    def complete_stem(self, tx, track_id):
        transaction = self._transaction(tx)
        self._track(track_id)
        if track_id not in transaction["completed"]:
            transaction["completed"].append(track_id)
        return {"tx": tx, "completed": list(transaction["completed"])}

    def commit_export(self, tx):
        transaction = self._transaction(tx)
        state = transaction["state"]
        stems = self._audible_ids(state)
        rendered = {
            track_id: self._render_track_samples(
                self._tracks[track_id], state[track_id]["gain_db"])
            for track_id in stems
        }
        transaction["committed"] = True
        return {"tx": tx, "stems": stems, "rendered": rendered}

    @staticmethod
    def _resample(samples, src_rate, dst_rate):
        if not samples:
            return []
        out_n = int(round(len(samples) * float(dst_rate) / float(src_rate)))
        if out_n <= 0:
            return []
        ratio = float(src_rate) / float(dst_rate)
        last = len(samples) - 1
        out = []
        for index in range(out_n):
            pos = index * ratio
            left = int(pos)
            frac = pos - left
            left_value = samples[min(left, last)]
            right_value = samples[min(left + 1, last)]
            out.append(left_value + (right_value - left_value) * frac)
        return out

    def _automation_end_sample(self, target_rate):
        end = 0
        for track in self._tracks.values():
            for when, _db in track.automation:
                end = max(end, when)
        return int(round(end * float(target_rate) / float(self.sample_rate)))

    @staticmethod
    def _write_atomic(path, payload, temp_files):
        temp_path = path + ".tmp"
        temp_files.append(temp_path)
        try:
            with open(temp_path, "wb") as handle:
                handle.write(payload)
            os.replace(temp_path, path)
        finally:
            if os.path.exists(temp_path):
                os.remove(temp_path)
            temp_files.remove(temp_path)

    def export(self, path, sample_rate=None, fail_disk=False):
        target_rate = int(sample_rate) if sample_rate else self.sample_rate
        master = list(self.render_master()["samples"])
        if self._limiter and master:
            current_peak = max(abs(value) for value in master)
            if current_peak > 1.0:
                gain = 1.0 / current_peak
                master = [value * gain for value in master]
        if target_rate != self.sample_rate:
            rendered = self._resample(master, self.sample_rate, target_rate)
        else:
            rendered = master
        peak = max((abs(value) for value in rendered), default=0.0)
        clipped = peak > 1.0
        automation_end = self._automation_end_sample(target_rate)
        result = {
            "path": path,
            "sample_rate": target_rate,
            "samples": len(rendered),
            "duration": len(rendered) / float(target_rate),
            "peak": peak,
            "clipped": clipped,
            "automation_end_sample": automation_end,
            "temp_files": [],
        }
        temp_files = []
        payload = struct.pack("<%df" % len(rendered), *rendered) if rendered else b""
        try:
            if fail_disk:
                raise OSError("disk full")
            self._write_atomic(path, payload, temp_files)
        except OSError as exc:
            for temp_path in list(temp_files):
                if os.path.exists(temp_path):
                    os.remove(temp_path)
                temp_files.remove(temp_path)
            result["status"] = "failed"
            result["error"] = MixdownError(EXPORT_FAILED, str(exc)).code
            return result
        self._exported = True
        result["status"] = "exported"
        return result

    def export_stems(self, directory):
        os.makedirs(directory, exist_ok=True)
        files = []
        used_names = set()
        for track_id in self._audible_ids():
            track = self._tracks[track_id]
            base = self._safe_name(track.name or track_id)
            candidate = base
            suffix = 1
            while candidate.lower() in used_names:
                suffix += 1
                candidate = "%s-%d" % (base, suffix)
            used_names.add(candidate.lower())
            path = os.path.join(directory, candidate + ".wav")
            samples = self._render_track_samples(track, track.gain_db)
            payload = struct.pack("<%df" % len(samples), *samples) if samples else b""
            temp_files = []
            self._write_atomic(path, payload, temp_files)
            files.append({"track": track_id, "path": path,
                          "samples": len(samples)})
        return {"directory": directory, "files": files}

    @staticmethod
    def _safe_name(name):
        cleaned = "".join(
            char if char.isalnum() or char in " ._-" else "_"
            for char in str(name)
        ).strip()
        return cleaned or "track"

    def export_status(self):
        return {"exported": self._exported}

    def recover_export(self, tx):
        transaction = self._transaction(tx)
        audible = self._audible_ids(transaction["state"])
        completed = list(transaction["completed"])
        pending = [track_id for track_id in audible
                   if track_id not in transaction["completed"]]
        return {"tx": tx, "completed": completed, "pending": pending}

    def set_limiter(self, enabled):
        self._limiter = bool(enabled)

    def peak(self):
        master = self.render_master()["samples"]
        return max((abs(value) for value in master), default=0.0)
