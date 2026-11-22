import math
import threading
from collections import deque


class TunerDeviceError(Exception):
    pass


_NOTE_NAMES = ("C", "C#", "D", "D#", "E", "F",
               "F#", "G", "G#", "A", "A#", "B")

_SUPPORTED_SAMPLE_RATES = frozenset((
    8000, 11025, 12000, 16000, 22050, 24000, 32000, 44100, 48000,
    64000, 88200, 96000, 176400, 192000, 352800, 384000,
))

_MIN_FREQUENCY = 30.0
_MAX_FREQUENCY = 2500.0
_MAX_HARMONIC_FREQUENCY = 8000.0
_SILENCE_RMS = 1.0e-5
_MIN_ANALYSIS_CYCLES = 2.0


def _next_power_of_two(value):
    result = 1
    while result < value:
        result <<= 1
    return result


def _fft(values, inverse=False):
    count = len(values)
    data = list(values)
    if count <= 1:
        return data

    target = 0
    for index in range(1, count):
        bit = count >> 1
        while target & bit:
            target ^= bit
            bit >>= 1
        target ^= bit
        if index < target:
            data[index], data[target] = data[target], data[index]

    length = 2
    direction = 1.0 if inverse else -1.0
    while length <= count:
        angle = direction * 2.0 * math.pi / length
        step = complex(math.cos(angle), math.sin(angle))
        half = length >> 1
        for start in range(0, count, length):
            factor = 1.0 + 0.0j
            stop = start + half
            for index in range(start, stop):
                even = data[index]
                odd = data[index + half] * factor
                data[index] = even + odd
                data[index + half] = even - odd
                factor *= step
        length <<= 1

    if inverse:
        scale = 1.0 / count
        for index in range(count):
            data[index] *= scale
    return data


def _hann_window(count):
    if count <= 1:
        return [1.0]
    denominator = float(count - 1)
    return [0.5 - 0.5 * math.cos(2.0 * math.pi * index / denominator)
            for index in range(count)]


def _midi_number(frequency, a4):
    return 69.0 + 12.0 * math.log(frequency / a4, 2.0)


def _frequency_annotation(frequency, a4):
    midi = _midi_number(frequency, a4)
    rounded = int(math.floor(midi + 0.5))
    note_index = rounded % 12
    octave = rounded // 12 - 1
    return {
        "frequency": float(frequency),
        "note": _NOTE_NAMES[note_index] + str(octave),
        "cents": float((midi - rounded) * 100.0),
        "midi": rounded,
        "target_frequency": float(a4 * (2.0 ** ((rounded - 69) / 12.0))),
    }


class PitchTuner:
    def __init__(self, sample_rate=44100, window_ms=80.0, a4=440.0,
                 confidence_threshold=0.8, latency_ms=0.0):
        sample_rate = self._validate_sample_rate(sample_rate)
        window_ms = self._coerce_positive_number(
            window_ms, "window length", ValueError)
        a4 = self._coerce_positive_number(
            a4, "A4 reference", ValueError)
        latency_ms = self._coerce_non_negative_number(latency_ms, "latency")

        self.sample_rate = sample_rate
        self.window_ms = float(window_ms)
        self.a4 = float(a4)
        self.confidence_threshold = float(confidence_threshold)
        self.latency_ms = float(latency_ms)

        self._mode = "single"
        self._buffer_lock = threading.RLock()
        self._analysis_lock = threading.RLock()
        self._result_lock = threading.RLock()
        self._history_lock = threading.RLock()
        self._buffer = deque(maxlen=self._buffer_capacity())
        self._result = self._undetected_result()
        self._analyzed_frames = 0
        self._last_frequency = None
        self._last_note_midi = None
        self._last_confidences = deque(maxlen=3)
        self._sample_serial = 0
        self._worker = None
        self._worker_stop = threading.Event()
        self._worker_wake = threading.Event()

    @staticmethod
    def _coerce_positive_number(value, name, error_type=TunerDeviceError):
        try:
            number = float(value)
        except (TypeError, ValueError) as exc:
            raise error_type("invalid " + name) from exc
        if not math.isfinite(number) or number <= 0.0:
            raise error_type("invalid " + name)
        return number

    @staticmethod
    def _coerce_non_negative_number(value, name):
        try:
            number = float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError("invalid " + name) from exc
        if not math.isfinite(number) or number < 0.0:
            raise ValueError("invalid " + name)
        return number

    def _buffer_capacity(self):
        return max(self.window_samples() * 4, 8192)

    def _undetected_result(self):
        return {
            "detected": False,
            "frequency": None,
            "note": None,
            "cents": None,
            "confidence": 0.0,
            "midi": None,
            "target_frequency": None,
            "notes": [],
            "mode": self._mode,
        }

    def _validate_sample_rate(self, sample_rate):
        try:
            numeric = float(sample_rate)
        except (TypeError, ValueError) as exc:
            raise TunerDeviceError("unsupported sample rate") from exc
        if not math.isfinite(numeric) or numeric <= 0.0:
            raise TunerDeviceError("unsupported sample rate")
        if not numeric.is_integer():
            raise TunerDeviceError("unsupported sample rate")
        rate = int(numeric)
        if rate not in _SUPPORTED_SAMPLE_RATES:
            raise TunerDeviceError("unsupported sample rate: " + str(sample_rate))
        return rate

    def open_device(self, sample_rate, device=None, opener=None):
        rate = self._validate_sample_rate(sample_rate)
        if opener is not None:
            try:
                opener(rate, device)
            except Exception as exc:
                raise TunerDeviceError("could not open audio device") from exc

        with self._buffer_lock:
            self.sample_rate = rate
            self._buffer = deque(maxlen=self._buffer_capacity())
        with self._analysis_lock:
            self._analyzed_frames = 0
            self._last_frequency = None
            self._last_note_midi = None
            self._last_confidences.clear()
        self._set_result(self._undetected_result())
        return self

    def push_samples(self, samples):
        if samples is None:
            return
        with self._buffer_lock:
            append = self._buffer.append
            for sample in samples:
                try:
                    value = float(sample)
                except (TypeError, ValueError):
                    try:
                        value = float(sample[0])
                    except (TypeError, ValueError, IndexError):
                        value = 0.0
                append(value)
            self._sample_serial += 1
        self._worker_wake.set()

    def window_samples(self):
        return max(1, int(round(self.sample_rate * self.window_ms / 1000.0)))

    def analyzed_frames(self):
        with self._analysis_lock:
            return self._analyzed_frames

    def set_a4(self, hz):
        value = self._coerce_positive_number(
            hz, "A4 reference", ValueError)
        self.a4 = value
        with self._result_lock:
            if self._result.get("detected"):
                notes = [self._reannotate_note(note) for note in self._result["notes"]]
                result = self._compose_result(notes, self._mode)
                self._result = result
        return self

    def set_mode(self, mode="single"):
        normalized = str(mode).strip().lower()
        if normalized == "chromatic":
            normalized = "single"
        if normalized not in ("single", "chord"):
            raise ValueError("unsupported tuner mode: " + str(mode))
        self._mode = normalized
        with self._result_lock:
            self._result["mode"] = normalized
        return self

    def set_latency_compensation(self, latency_ms):
        self.latency_ms = self._coerce_non_negative_number(
            latency_ms, "latency")
        return self

    def set_latency(self, latency_ms):
        return self.set_latency_compensation(latency_ms)

    def set_input_latency(self, latency_ms):
        return self.set_latency_compensation(latency_ms)

    def reset(self):
        with self._buffer_lock:
            self._buffer.clear()
        with self._analysis_lock:
            self._analyzed_frames = 0
            self._last_frequency = None
            self._last_note_midi = None
            self._last_confidences.clear()
        self._set_result(self._undetected_result())
        return self

    def analyze(self):
        with self._analysis_lock:
            with self._buffer_lock:
                data = list(self._buffer)
            window = self.window_samples()
            if len(data) < window:
                result = self._undetected_result()
                self._clear_smoothing()
                self._set_result(result)
                return self._copy_result(result)

            block = data[-window:]
            result = self._analyze_window(block)
            self._analyzed_frames += 1
            self._set_result(result)
            return self._copy_result(result)

    def analyze_async(self):
        self._worker_wake.set()
        if not self.is_running():
            self.start()

    def start(self):
        if self.is_running():
            return self
        self._worker_stop.clear()
        self._worker_wake.clear()
        self._worker = threading.Thread(
            target=self._analysis_loop,
            name="PitchTunerAnalysis",
            daemon=True,
        )
        self._worker.start()
        return self

    def start_analysis(self):
        return self.start()

    def start_background_analysis(self):
        return self.start()

    def stop(self):
        worker = self._worker
        if worker is None:
            return self
        self._worker_stop.set()
        self._worker_wake.set()
        if worker is not threading.current_thread():
            worker.join(timeout=1.0)
        self._worker = None
        return self

    def stop_analysis(self):
        return self.stop()

    def close(self):
        return self.stop()

    def is_running(self):
        worker = self._worker
        return bool(worker is not None and worker.is_alive())

    def _analysis_loop(self):
        last_serial = -1
        interval = max(0.005, min(0.05, self.window_ms / 4000.0))
        while not self._worker_stop.is_set():
            self._worker_wake.wait(timeout=interval)
            self._worker_wake.clear()
            if self._worker_stop.is_set():
                break
            with self._buffer_lock:
                serial = self._sample_serial
            if serial == last_serial:
                continue
            last_serial = serial
            self.analyze()

    def snapshot(self):
        with self._result_lock:
            return self._copy_result(self._result)

    def latest_result(self):
        return self.snapshot()

    def current_result(self):
        return self.snapshot()

    def get_result(self):
        return self.snapshot()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.stop()
        return False

    def _copy_result(self, result):
        copied = dict(result)
        copied["notes"] = [dict(note) for note in result.get("notes", [])]
        return copied

    def _set_result(self, result):
        with self._result_lock:
            self._result = self._copy_result(result)

    def _reannotate_note(self, note):
        updated = _frequency_annotation(note["frequency"], self.a4)
        updated["confidence"] = note.get("confidence", 0.0)
        return updated

    def _compose_result(self, notes, mode):
        if not notes:
            result = self._undetected_result()
            result["mode"] = mode
            return result
        ordered = sorted(notes, key=lambda item: item["frequency"])
        primary = ordered[0]
        result = {
            "detected": True,
            "frequency": primary["frequency"],
            "note": primary["note"],
            "cents": primary["cents"],
            "confidence": primary["confidence"],
            "midi": primary["midi"],
            "target_frequency": primary["target_frequency"],
            "notes": [dict(item) for item in ordered],
            "mode": mode,
        }
        return result

    def _analyze_window(self, samples):
        prepared = self._prepare_samples(samples)
        if prepared is None:
            self._clear_smoothing()
            return self._undetected_result()
        signal, rms = prepared
        if rms < _SILENCE_RMS:
            self._clear_smoothing()
            return self._undetected_result()

        spectrum = self._build_spectrum(signal)
        if spectrum is None:
            self._clear_smoothing()
            return self._undetected_result()

        if self._mode == "chord":
            notes = self._detect_chord(signal, spectrum)
        else:
            notes = self._detect_single(signal, spectrum)

        accepted = [note for note in notes
                    if note["confidence"] >= self.confidence_threshold]
        if not accepted:
            if not self._noise_like(spectrum, notes):
                with self._result_lock:
                    previous = self._copy_result(self._result)
                if (previous.get("detected")
                        and previous.get("mode") == self._mode):
                    return previous
            self._clear_smoothing()
            return self._undetected_result()
        notes = accepted
        if self._mode == "single":
            notes = [self._smooth_note(notes[0])]
        else:
            notes = [self._smooth_chord_note(note) for note in notes]
        return self._compose_result(notes, self._mode)

    def _prepare_samples(self, samples):
        count = len(samples)
        if count < 16:
            return None
        cleaned = []
        total = 0.0
        for value in samples:
            if not math.isfinite(value):
                value = 0.0
            cleaned.append(value)
            total += value
        mean = total / count
        energy = 0.0
        for index in range(count):
            value = cleaned[index] - mean
            cleaned[index] = value
            energy += value * value
        rms = math.sqrt(energy / count)
        return cleaned, rms

    def _build_spectrum(self, signal):
        count = len(signal)
        fft_size = _next_power_of_two(count * 2)
        window = _hann_window(count)
        padded = [complex(signal[index] * window[index], 0.0)
                  for index in range(count)]
        padded.extend([0.0j] * (fft_size - count))
        transformed = _fft(padded)

        half = fft_size // 2
        power = [0.0] * (half + 1)
        for index in range(half + 1):
            value = transformed[index]
            power[index] = value.real * value.real + value.imag * value.imag

        raw = [complex(value, 0.0) for value in signal]
        raw.extend([0.0j] * (fft_size - count))
        raw_transformed = _fft(raw)
        full_power = [0.0] * fft_size
        for index in range(fft_size):
            value = raw_transformed[index]
            full_power[index] = value.real * value.real + value.imag * value.imag
        autocorrelation = _fft(full_power, inverse=True)

        squared = [0.0] * (count + 1)
        for index in range(count):
            squared[index + 1] = squared[index] + signal[index] * signal[index]

        return {
            "fft_size": fft_size,
            "bin_hz": self.sample_rate / float(fft_size),
            "power": power,
            "autocorrelation": [value.real for value in autocorrelation],
            "squared": squared,
            "sample_count": count,
        }

    def _periodicity(self, spectrum, lag):
        sample_count = spectrum["sample_count"]
        if lag < 1 or lag >= sample_count:
            return 0.0
        denominator = spectrum["squared"][sample_count - lag]
        if denominator <= 1.0e-18:
            return 0.0
        value = spectrum["autocorrelation"][lag] / denominator
        return max(0.0, min(1.0, value))

    def _band_energy(self, spectrum, frequency, relative_width=0.008):
        power = spectrum["power"]
        bin_hz = spectrum["bin_hz"]
        half_width = max(1.5 * bin_hz, relative_width * frequency)
        low = max(0, int(math.floor((frequency - half_width) / bin_hz)))
        high = min(len(power) - 1,
                   int(math.ceil((frequency + half_width) / bin_hz)))
        if high < low:
            return 0.0
        return sum(power[low:high + 1])

    def _total_energy(self, spectrum):
        power = spectrum["power"]
        bin_hz = spectrum["bin_hz"]
        low = max(1, int(math.floor(_MIN_FREQUENCY / bin_hz)))
        nyquist = self.sample_rate / 2.0
        upper = min(_MAX_HARMONIC_FREQUENCY, nyquist * 0.98)
        high = min(len(power) - 1, int(math.ceil(upper / bin_hz)))
        return sum(power[low:high + 1])

    def _spectral_peaks(self, spectrum, low_frequency, high_frequency,
                        maximum=24):
        power = spectrum["power"]
        bin_hz = spectrum["bin_hz"]
        low = max(2, int(math.floor(low_frequency / bin_hz)))
        high = min(len(power) - 2, int(math.ceil(high_frequency / bin_hz)))
        if high <= low:
            return []
        strongest = max(power[low:high + 1], default=0.0)
        floor = max(strongest * 1.0e-6, 1.0e-18)
        peaks = []
        for index in range(low, high + 1):
            value = power[index]
            if value < floor:
                continue
            if value < power[index - 1] or value < power[index + 1]:
                continue
            if value == power[index - 1] and value == power[index + 1]:
                continue
            left = power[index - 1]
            right = power[index + 1]
            floor = max(max(left, value, right) * 1.0e-12, 1.0e-30)
            left_log = math.log(max(left, floor))
            value_log = math.log(max(value, floor))
            right_log = math.log(max(right, floor))
            denominator = left_log - 2.0 * value_log + right_log
            offset = 0.0
            if abs(denominator) > 1.0e-30:
                offset = 0.5 * (left_log - right_log) / denominator
                offset = max(-0.5, min(0.5, offset))
            frequency = (index + offset) * bin_hz
            if frequency < low_frequency or frequency > high_frequency:
                continue
            strength = value * (1.0 - abs(offset) * 0.5)
            peaks.append((frequency, strength))
        peaks.sort(key=lambda item: item[1], reverse=True)
        peaks = peaks[:maximum]
        peaks.sort(key=lambda item: item[0])
        return peaks

    def _spectral_flatness(self, spectrum):
        power = spectrum["power"]
        bin_hz = spectrum["bin_hz"]
        low = max(1, int(math.floor(50.0 / bin_hz)))
        high = min(len(power) - 1, int(math.ceil(4000.0 / bin_hz)))
        if high <= low:
            return 1.0
        values = power[low:high + 1]
        total = sum(values)
        if total <= 0.0:
            return 1.0
        floor = max(total / len(values) * 1.0e-8, 1.0e-20)
        log_sum = 0.0
        for value in values:
            log_sum += math.log(max(value, floor))
        geometric = math.exp(log_sum / len(values))
        arithmetic = total / len(values)
        return geometric / arithmetic if arithmetic > 0.0 else 1.0

    def _candidate_frequencies(self, spectrum):
        candidates = {}

        def add(frequency, quality):
            if not math.isfinite(frequency):
                return
            if frequency < _MIN_FREQUENCY or frequency > _MAX_FREQUENCY:
                return
            for existing in list(candidates):
                cents = abs(1200.0 * math.log(frequency / existing, 2.0))
                if cents < 8.0:
                    if quality > candidates[existing]:
                        del candidates[existing]
                        candidates[frequency] = quality
                    return
            candidates[frequency] = max(quality,
                                        candidates.get(frequency, 0.0))

        sample_count = spectrum["sample_count"]
        rate = float(self.sample_rate)
        squared = spectrum["squared"]
        autocorrelation = spectrum["autocorrelation"]
        low_lag = max(2, int(math.floor(rate / _MAX_FREQUENCY)))
        high_lag = min(sample_count - 2,
                       int(math.ceil(rate / _MIN_FREQUENCY)))
        for lag in range(low_lag + 1, high_lag):
            denominator = squared[sample_count - lag]
            if denominator <= 1.0e-18:
                continue
            current = max(0.0, min(1.0,
                                     autocorrelation[lag] / denominator))
            if current < 0.12:
                continue
            before = squared[sample_count - lag + 1]
            after = squared[sample_count - lag - 1]
            left = (max(0.0, min(1.0, autocorrelation[lag - 1] / before))
                    if before > 1.0e-18 else 0.0)
            right = (max(0.0, min(1.0, autocorrelation[lag + 1] / after))
                     if after > 1.0e-18 else 0.0)
            if current < left or current < right:
                continue
            denominator_parabola = left - 2.0 * current + right
            offset = 0.0
            if abs(denominator_parabola) > 1.0e-18:
                offset = 0.5 * (left - right) / denominator_parabola
                offset = max(-0.5, min(0.5, offset))
            refined_lag = lag + offset
            if refined_lag <= 0.0:
                continue
            frequency = rate / refined_lag
            add(frequency, current)

        peaks = self._spectral_peaks(
            spectrum, _MIN_FREQUENCY, _MAX_FREQUENCY, maximum=20)
        maximum_power = max((strength for _, strength in peaks), default=0.0)
        for frequency, strength in peaks:
            quality = (strength / maximum_power + 1.0e-6
                       if maximum_power else 0.0)
            add(frequency, quality)
            if maximum_power > 0.0 and strength >= maximum_power * 0.03:
                for divisor in (2, 3):
                    sub_frequency = frequency / divisor
                    if sub_frequency >= _MIN_FREQUENCY:
                        add(sub_frequency, 0.55 * strength / maximum_power)
        return list(candidates.items())

    def _score_candidate(self, frequency, spectrum, total_energy):
        if total_energy <= 0.0:
            return 0.0
        nyquist = self.sample_rate / 2.0
        upper = min(_MAX_HARMONIC_FREQUENCY, nyquist * 0.98)
        harmonic_count = max(1, min(18, int(upper / frequency)))
        explained = 0.0
        for harmonic in range(1, harmonic_count + 1):
            harmonic_frequency = frequency * harmonic
            if harmonic_frequency > upper:
                break
            explained += self._band_energy(spectrum, harmonic_frequency)
        explained_ratio = min(1.0, explained / total_energy)

        fundamental_energy = self._band_energy(spectrum, frequency)
        peaks = self._spectral_peaks(
            spectrum, _MIN_FREQUENCY, _MAX_FREQUENCY, maximum=1)
        strongest = peaks[0][1] if peaks else 0.0
        if strongest > 0.0:
            presence = min(1.0, fundamental_energy / strongest)
        else:
            presence = 0.0
        lag = int(round(self.sample_rate / frequency))
        best_periodicity = 0.0
        for offset in (-1, 0, 1):
            candidate_lag = lag + offset
            if candidate_lag > 0:
                best_periodicity = max(
                    best_periodicity,
                    self._periodicity(spectrum, candidate_lag),
                )

        flatness = self._spectral_flatness(spectrum)
        tonal_factor = max(0.0, 1.0 - flatness)
        base_score = (0.80 * explained_ratio
                      + 0.08 * presence
                      + 0.10 * best_periodicity
                      + 0.02 * tonal_factor)
        score = base_score * (0.40 + 0.60 * best_periodicity)
        return max(0.0, min(1.0, score))

    def _detect_single(self, signal, spectrum):
        total_energy = self._total_energy(spectrum)
        if total_energy <= 1.0e-18:
            return []
        best = None
        for frequency, quality in self._candidate_frequencies(spectrum):
            score = self._score_candidate(frequency, spectrum, total_energy)
            if best is None or score > best[0] + 1.0e-3:
                best = (score, frequency, quality)
            elif abs(score - best[0]) <= 1.0e-3:
                if frequency > best[1] + 1.0e-9:
                    best = (score, frequency, quality)
                elif (abs(frequency - best[1]) <= 1.0e-9
                      and quality > best[2]):
                    best = (score, frequency, quality)
        if best is None:
            return []
        confidence, frequency, _ = best
        duration = spectrum["sample_count"] / float(self.sample_rate)
        if frequency * duration < _MIN_ANALYSIS_CYCLES:
            return []
        periodicity = max(
            self._periodicity(spectrum, int(round(self.sample_rate / frequency)) + offset)
            for offset in (-1, 0, 1)
        )
        note = _frequency_annotation(frequency, self.a4)
        note["confidence"] = confidence
        note["periodicity"] = periodicity
        return [note]

    def _noise_like(self, spectrum, notes=None):
        if notes:
            periodicities = []
            for note in notes:
                if "periodicity" in note:
                    periodicities.append(note["periodicity"])
                else:
                    lag = int(round(self.sample_rate / note["frequency"]))
                    periodicities.append(self._periodicity(spectrum, lag))
            if periodicities and max(periodicities) < 0.18:
                return True
        else:
            peaks = self._spectral_peaks(
                spectrum, _MIN_FREQUENCY, _MAX_FREQUENCY, maximum=8)
            periodicities = [
                self._periodicity(
                    spectrum,
                    int(round(self.sample_rate / frequency)),
                )
                for frequency, _ in peaks
            ]
            if periodicities and max(periodicities) < 0.15:
                return True
        if self._spectral_flatness(spectrum) >= 0.35:
            return True
        total_energy = self._total_energy(spectrum)
        if total_energy <= 1.0e-18:
            return True
        peaks = self._spectral_peaks(
            spectrum, _MIN_FREQUENCY, _MAX_FREQUENCY, maximum=8)
        concentration = sum(strength for _, strength in peaks) / total_energy
        return concentration < 0.03

    @staticmethod
    def _harmonic_relation(lower, upper):
        ratio = upper / lower
        if ratio <= 1.05:
            return None
        nearest = int(round(ratio))
        if nearest < 2:
            return None
        cents = abs(1200.0 * math.log(ratio / nearest, 2.0))
        if cents > 35.0:
            return None
        return nearest

    def _detect_chord(self, signal, spectrum):
        total_energy = self._total_energy(spectrum)
        if total_energy <= 1.0e-18:
            return []
        if self._spectral_flatness(spectrum) >= 0.05:
            return []
        peaks = self._spectral_peaks(
            spectrum, _MIN_FREQUENCY, _MAX_FREQUENCY, maximum=32)
        if not peaks:
            return []
        strongest = max(strength for _, strength in peaks)
        if strongest <= 0.0:
            return []

        candidates = []
        for frequency, strength in peaks:
            if strength < strongest * 2.0e-4:
                continue
            concentration = strength / total_energy
            relative = strength / strongest
            confidence = (0.40
                          + 0.40 * min(1.0, relative * 2.5)
                          + 0.20 * min(1.0, concentration * 20.0))
            candidates.append({
                "frequency": frequency,
                "strength": strength,
                "confidence": min(1.0, confidence),
            })
        if not candidates:
            return []
        candidate_periodicities = []
        for candidate in candidates:
            lag = int(round(self.sample_rate / candidate["frequency"]))
            candidate["periodicity"] = self._periodicity(spectrum, lag)
            candidate_periodicities.append(candidate["periodicity"])
        if max(candidate_periodicities, default=0.0) < 0.15:
            return []

        candidates.sort(key=lambda item: item["frequency"])
        selected = []
        for candidate in candidates:
            harmonic_of = None
            for lower in selected:
                relation = self._harmonic_relation(
                    lower["frequency"], candidate["frequency"])
                if relation is not None:
                    harmonic_of = (lower, relation)
                    break
            if harmonic_of is None:
                selected.append(candidate)
                continue

            lower, harmonic = harmonic_of
            predicted = lower["strength"] / (harmonic ** 0.8)
            if candidate["strength"] > predicted * 2.2:
                selected.append(candidate)

        notes = []
        for candidate in selected:
            duration = spectrum["sample_count"] / float(self.sample_rate)
            if candidate["frequency"] * duration < _MIN_ANALYSIS_CYCLES:
                continue
            note = _frequency_annotation(candidate["frequency"], self.a4)
            note["confidence"] = candidate["confidence"]
            note["periodicity"] = candidate["periodicity"]
            notes.append(note)
        notes.sort(key=lambda item: item["frequency"])
        return notes[:8]

    def _clear_smoothing(self):
        with self._history_lock:
            self._last_frequency = None
            self._last_note_midi = None
            self._last_confidences.clear()

    def _smooth_note(self, note):
        with self._history_lock:
            frequency = note["frequency"]
            midi = note["midi"]
            if (self._last_frequency is not None
                    and self._last_note_midi == midi):
                delta = abs(1200.0 * math.log(
                    frequency / self._last_frequency, 2.0))
                if delta <= 5.0:
                    frequency = 0.65 * self._last_frequency + 0.35 * frequency
            self._last_frequency = frequency
            self._last_note_midi = midi
            self._last_confidences.append(note["confidence"])
        smoothed = _frequency_annotation(frequency, self.a4)
        smoothed["confidence"] = note["confidence"]
        return smoothed

    def _smooth_chord_note(self, note):
        return note
