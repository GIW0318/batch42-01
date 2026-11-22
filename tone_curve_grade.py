class GradeConfigError(Exception):
    pass


_MIN_EXPOSURE = 1e-4
_MIN_LUMINANCE = 1e-6


def _clamp(value, lo, hi):
    return lo if value < lo else hi if value > hi else value


class ToneGrade:
    """HDR grading pipeline.

    The grade is always applied as three fixed steps, in order:
    exposure -> tonemap -> grading LUT. UI/text bypass all three and
    are composited afterwards.
    """

    def __init__(self, lut_size=16, exposure_time_ms=300.0, gamma=2.2):
        if lut_size < 1:
            raise GradeConfigError("lut_size must be >= 1")
        if exposure_time_ms < 0:
            raise GradeConfigError("exposure_time_ms must be >= 0")
        if gamma <= 0:
            raise GradeConfigError("gamma must be > 0")
        self.lut_size = lut_size
        self.exposure_time_ms = exposure_time_ms
        self.gamma = gamma
        self._lut = {}
        self._exposure = 1.0
        self._scene_id = None
        self._scene_metering = 0.5

    def set_lut(self, entries, size=None):
        size = self.lut_size if size is None else size
        if size < 1:
            raise GradeConfigError("lut size must be >= 1")
        self.lut_size = size
        self._lut = dict(entries)

    def sample_lut(self, color):
        size = self.lut_size
        r, g, b = (_clamp(float(c), 0.0, 1.0) for c in color)
        x = r * (size - 1)
        y = g * (size - 1)
        z = b * (size - 1)
        x0 = int(x)
        y0 = int(y)
        z0 = int(z)
        x1 = min(x0 + 1, size - 1)
        y1 = min(y0 + 1, size - 1)
        z1 = min(z0 + 1, size - 1)
        fx = x - x0
        fy = y - y0
        fz = z - z0

        denom = max(size - 1, 1)

        def cell(i, j, k):
            return self._lut.get(
                (i, j, k), (i / denom, j / denom, k / denom)
            )

        c000 = cell(x0, y0, z0)
        c100 = cell(x1, y0, z0)
        c010 = cell(x0, y1, z0)
        c110 = cell(x1, y1, z0)
        c001 = cell(x0, y0, z1)
        c101 = cell(x1, y0, z1)
        c011 = cell(x0, y1, z1)
        c111 = cell(x1, y1, z1)

        def lerp(a, b, t):
            return a + (b - a) * t

        out = []
        for ch in range(3):
            v00 = lerp(c000[ch], c100[ch], fx)
            v10 = lerp(c010[ch], c110[ch], fx)
            v01 = lerp(c001[ch], c101[ch], fx)
            v11 = lerp(c011[ch], c111[ch], fx)
            v0 = lerp(v00, v10, fy)
            v1 = lerp(v01, v11, fy)
            out.append(lerp(v0, v1, fz))
        return tuple(out)

    def auto_exposure(self, scene_luminance, dt_ms):
        dt_ms = max(float(dt_ms), 0.0)
        target = 1.0 / max(float(scene_luminance), _MIN_LUMINANCE)
        if self.exposure_time_ms <= 0:
            alpha = 1.0
        else:
            alpha = min(dt_ms / self.exposure_time_ms, 1.0)
        self._exposure += (target - self._exposure) * alpha
        return self._exposure

    def exposure(self):
        return self._exposure

    def grade(self, color, exposure=1.0, ui=False):
        if ui:
            return tuple(color)
        exposure = max(float(exposure), _MIN_EXPOSURE)
        scaled = tuple(float(c) * exposure for c in color)
        return self.sample_lut(self.tonemap(scaled))

    def tonemap(self, color):
        return tuple(c / (1.0 + c) if c > 0.0 else 0.0 for c in color)

    def hdr_to_sdr(self, color):
        inv_gamma = 1.0 / self.gamma
        return tuple(
            _clamp(float(c), 0.0, 1.0) ** inv_gamma for c in color
        )

    def set_scene(self, scene_id, lut=None, metering=0.5):
        self._scene_id = scene_id
        self._scene_metering = float(metering)
        if lut is not None:
            self.set_lut(lut)
        self._exposure = 1.0 / max(self._scene_metering, _MIN_LUMINANCE)

    def end_cutscene(self):
        self._exposure = 1.0 / max(self._scene_metering, _MIN_LUMINANCE)

    def composite_layers(self, layers):
        layers = list(layers)
        if not layers:
            return (0.0, 0.0, 0.0)
        count = len(layers)
        blended = tuple(
            sum(layer[ch] for layer in layers) / count for ch in range(3)
        )
        return self.tonemap(blended)
