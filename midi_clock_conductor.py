class MidiDeviceError(Exception):
    pass


class _Port:
    def __init__(self, name):
        self.name = name
        self.connected = True
        self.sent = []

    def send(self, message):
        self.sent.append(message)


class MidiConductor:
    """Clock, event queue and output ports kept as separate concerns.

    The conductor owns a single monotonic millisecond time base shared by
    every output port.  Scheduled events keep their position in beats and are
    converted to a sample-level millisecond timestamp lazily (against the
    current tempo map) when they are flushed, so a tempo change can never
    re-time events that were already due.
    """

    def __init__(self, tempo_bpm=120.0, ppq=480):
        self.tempo_bpm = tempo_bpm
        self.ppq = ppq
        self._ports = {}
        self._events = []
        self._tempo_map = [(0.0, 0.0, float(tempo_bpm))]
        self._playing = False
        self._paused = False
        self._now_ms = 0.0
        self._loop_origin_ms = 0.0
        self._loop_count = 0
        self._external_clock = False
        self._jitter_ms = 0.0
        self._last_external_ms = 0.0
        self._seq = 0
        self._active = {}
        self._held = {}
        self._velocities = {}
        self._closed = False

    # ------------------------------------------------------------------ ports
    def add_port(self, name):
        port = self._ports.get(name)
        if port is None:
            port = _Port(name)
            self._ports[name] = port
        port.connected = True
        self._active.setdefault(name, {})
        self._held.setdefault(name, {})
        self._velocities.setdefault(name, {})
        return port

    def remove_port(self, name):
        port = self._ports.pop(name, None)
        if port is not None:
            port.connected = False
        self._events = [event for event in self._events
                        if event["port"] != name]
        self._active.pop(name, None)
        self._held.pop(name, None)
        self._velocities.pop(name, None)

    def _require_port(self, name):
        port = self._ports.get(name)
        if port is None or not port.connected:
            raise MidiDeviceError("MIDI output port not bound: %r" % name)
        return port

    # ------------------------------------------------------------- tempo map
    def _ms_per_beat(self, bpm):
        return 60000.0 / float(bpm)

    def _beat_to_ms(self, beat):
        """Convert a beat position to conductor ms using the tempo map."""
        segment = self._tempo_map[-1]
        for entry in self._tempo_map:
            if entry[0] <= beat:
                segment = entry
            else:
                break
        seg_beat, seg_ms, bpm = segment
        return seg_ms + (beat - seg_beat) * self._ms_per_beat(bpm)

    def _ms_to_beat(self, timestamp_ms):
        segment = self._tempo_map[-1]
        for entry in self._tempo_map:
            if entry[1] <= timestamp_ms:
                segment = entry
            else:
                break
        seg_beat, seg_ms, bpm = segment
        return seg_beat + (timestamp_ms - seg_ms) / self._ms_per_beat(bpm)

    def set_tempo(self, bpm, at_ms):
        # Tempo changes only re-time events that come after the change point.
        at_beat = self._ms_to_beat(float(at_ms))
        at_ms = float(at_ms)
        existing = [index for index, entry in enumerate(self._tempo_map)
                    if entry[1] == at_ms]
        if existing:
            for index in existing:
                entry = self._tempo_map[index]
                self._tempo_map[index] = (entry[0], entry[1], float(bpm))
        else:
            self._tempo_map.append((at_beat, at_ms, float(bpm)))
        self._tempo_map.sort(key=lambda entry: entry[1])
        rebuilt = []
        for index, (beat, ms, entry_bpm) in enumerate(self._tempo_map):
            if index == 0:
                rebuilt.append((0.0, self._loop_origin_ms, entry_bpm))
            else:
                prev_beat, prev_ms, prev_bpm = rebuilt[-1]
                beat = prev_beat + (ms - prev_ms) / self._ms_per_beat(prev_bpm)
                rebuilt.append((beat, ms, entry_bpm))
        self._tempo_map = rebuilt
        self.tempo_bpm = float(bpm)

    # -------------------------------------------------------------- scheduling
    def schedule_note(self, port, note, velocity, beat, duration_beats):
        self._require_port(port)
        on_event = {
            "port": port,
            "note": note,
            "velocity": velocity,
            "beat": float(beat),
            "kind": "note_on",
            "seq": self._seq,
        }
        off_event = {
            "port": port,
            "note": note,
            "velocity": 0,
            "beat": float(beat) + float(duration_beats),
            "kind": "note_off",
            "seq": self._seq + 1,
        }
        self._seq += 2
        self._events.append(on_event)
        self._events.append(off_event)

    # ------------------------------------------------------------- transport
    def play(self):
        if self._closed:
            raise MidiDeviceError("Conductor has been closed")
        if self._paused:
            # Resume: re-arm the notes that were forced off while paused so
            # they keep sounding without a timestamp jump.
            self._paused = False
            held = self._held
            self._held = {}
            resume = []
            for port_name, notes in held.items():
                port = self._ports.get(port_name)
                if port is None:
                    continue
                for note, velocities in notes.items():
                    for velocity in velocities:
                        self._active.setdefault(port_name, {})[note] = \
                            self._active.get(port_name, {}).get(note, 0) + 1
                        self._velocities.setdefault(
                            port_name, {}).setdefault(note, []).append(velocity)
                        resume.append(self._make_message(
                            port_name, "note_on", note, velocity, self._now_ms))
            resume.sort(key=lambda message: message["seq"])
            self._playing = True
            return resume
        self._playing = True
        return []

    def _make_message(self, port_name, kind, note, velocity, at_ms, seq=None):
        if seq is None:
            seq = self._seq
            self._seq += 1
        return {
            "type": kind,
            "port": port_name,
            "note": note,
            "velocity": velocity,
            "at_ms": at_ms,
            "seq": seq,
        }

    def _panic(self):
        """Force every sounding note off and emit all-notes-off per port."""
        messages = []
        for port_name in sorted(self._active):
            counts = self._active[port_name]
            if not counts:
                continue
            for note in sorted(counts):
                count = counts[note]
                for _ in range(count):
                    messages.append(self._make_message(
                        port_name, "note_off", note, 0, self._now_ms))
            for note, velocities in self._velocities.get(port_name, {}).items():
                self._held.setdefault(port_name, {}).setdefault(
                    note, []).extend(velocities)
            self._velocities.get(port_name, {}).clear()
            counts.clear()
        return messages

    def pause(self):
        if not self._playing or self._paused:
            return []
        self._paused = True
        self._playing = False
        messages = self._panic()
        return messages

    def stop(self):
        messages = self._panic()
        messages.extend(self._all_notes_off_for_bound_ports())
        self._events = []
        self._held = {}
        self._playing = False
        self._paused = False
        self._now_ms = 0.0
        self._loop_origin_ms = 0.0
        self._loop_count = 0
        self._tempo_map = [(0.0, 0.0, float(self.tempo_bpm))]
        return messages

    def close(self):
        if self._closed:
            return []
        messages = self._panic()
        messages.extend(self._all_notes_off_for_bound_ports())
        self._events = []
        self._held = {}
        self._playing = False
        self._paused = False
        self._closed = True
        for port in self._ports.values():
            port.connected = False
        return messages

    def _all_notes_off_for_bound_ports(self):
        messages = []
        for port_name in sorted(self._ports):
            messages.append({
                "type": "all_notes_off",
                "port": port_name,
                "note": None,
                "velocity": 0,
                "at_ms": self._now_ms,
                "seq": self._seq,
            })
            self._seq += 1
        return messages

    # -------------------------------------------------------------- external
    def set_external_clock(self, enabled, jitter_ms=0.0):
        self._external_clock = bool(enabled)
        self._jitter_ms = float(jitter_ms)

    def external_tick(self, timestamp_ms):
        if not self._external_clock or self._paused or not self._playing:
            return []
        timestamp_ms = float(timestamp_ms)
        # External device time is authoritative; tolerate jitter without
        # ever letting the monotonic clock run backwards.
        if timestamp_ms < self._now_ms - self._jitter_ms:
            timestamp_ms = self._now_ms
        self._last_external_ms = timestamp_ms
        return self._advance_to(timestamp_ms)

    # ---------------------------------------------------------------- engine
    def advance(self, target_ms):
        if not self._playing or self._paused:
            return []
        if self._external_clock:
            # External clock owns the time base: advance() must not free-run.
            return self._advance_to(self._now_ms)
        return self._advance_to(float(target_ms))

    def _advance_to(self, target_ms):
        if target_ms < self._now_ms:
            target_ms = self._now_ms
        due = []
        remaining = []
        for event in self._events:
            at_ms = self._beat_to_ms(event["beat"])
            if at_ms <= target_ms:
                due.append((at_ms, event))
            else:
                remaining.append(event)
        # Timestamp order first, submission order (seq) as tie breaker.
        due.sort(key=lambda item: (item[0], item[1]["seq"]))
        messages = []
        for at_ms, event in due:
            port = self._ports.get(event["port"])
            if port is None or not port.connected:
                continue
            message = self._make_message(
                event["port"], event["kind"], event["note"],
                event["velocity"], at_ms, seq=event["seq"])
            messages.append(message)
            port.send(message)
            counts = self._active.setdefault(event["port"], {})
            if event["kind"] == "note_on":
                counts[event["note"]] = counts.get(event["note"], 0) + 1
                self._velocities.setdefault(event["port"], {}).setdefault(
                    event["note"], []).append(event["velocity"])
            else:
                count = counts.get(event["note"], 0)
                if count > 0:
                    count -= 1
                    if count:
                        counts[event["note"]] = count
                    else:
                        del counts[event["note"]]
                    stack = self._velocities.get(
                        event["port"], {}).get(event["note"])
                    if stack:
                        stack.pop(0)
        self._events = remaining
        self._now_ms = target_ms
        return messages

    # ------------------------------------------------------------------ loop
    def loop(self):
        # Anything still queued belongs to the previous pass; make sure no
        # sounding note is left hanging across the wrap.
        messages = self._panic()
        messages.extend(self._all_notes_off_for_bound_ports())
        self._events = []
        self._held = {}
        start_ms = max(self._now_ms, self._loop_origin_ms + 1.0)
        self._loop_origin_ms = start_ms
        self._now_ms = start_ms
        self._loop_count += 1
        self._tempo_map = [(0.0, start_ms, float(self.tempo_bpm))]
        return {
            "start_ms": start_ms,
            "index": self._loop_count,
            "messages": messages,
        }

    # ---------------------------------------------------------------- queries
    def active_notes(self, port):
        counts = self._active.get(port, {})
        notes = []
        for note in sorted(counts):
            notes.extend([note] * counts[note])
        return notes

    def now_ms(self):
        return self._now_ms

    def quantize(self, tick, grid):
        """Snap to the nearest grid line, so weak pickups keep their beat."""
        grid = int(grid)
        tick = int(tick)
        return ((tick + grid // 2) // grid) * grid
