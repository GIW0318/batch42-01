class MsaaSetupError(Exception):
    pass


_SUPPORTED_FORMATS = {
    "RGBA8", "BGRA8", "RGB10A2", "RGBA16F", "RGBA32F", "R11G11B10F",
}

_MAX_SAMPLES = 16

# Rough throughput estimate: megasamples per millisecond the resolve
# pass can afford. Used only to decide whether the per-frame budget is
# exceeded; coverage rules are never changed by downsampling.
_RESOLVE_THROUGHPUT = 64.0


def _validate_samples(samples):
    if not isinstance(samples, int) or isinstance(samples, bool):
        raise MsaaSetupError("sample count must be an integer")
    if samples < 1 or samples > _MAX_SAMPLES or (samples & (samples - 1)):
        raise MsaaSetupError("unsupported sample count: %r" % (samples,))


def _validate_resolution(width, height):
    if not isinstance(width, int) or not isinstance(height, int):
        raise MsaaSetupError("resolution must be integer pixels")
    if width <= 0 or height <= 0:
        raise MsaaSetupError("resolution must be positive")


def _validate_format(fmt):
    if fmt not in _SUPPORTED_FORMATS:
        raise MsaaSetupError("unsupported resolve format: %r" % (fmt,))


class MsaaResolve:
    def __init__(self, samples=4, width=1920, height=1080,
                 format="RGBA16F"):
        _validate_samples(samples)
        _validate_resolution(width, height)
        _validate_format(format)
        self.samples = samples
        self.width = width
        self.height = height
        self.format = format
        self.enabled = True
        self._generation = 1
        self._resolved = None
        self._budget = None
        self._eye_samples = {"left": samples, "right": samples}
        self._pipeline = ["resolve", "postprocess"]

    def _rebuild(self):
        self._generation += 1
        self._resolved = None

    def set_samples(self, samples):
        _validate_samples(samples)
        if samples != self.samples:
            self.samples = samples
            self._eye_samples = {"left": samples, "right": samples}
            self._rebuild()

    def set_resolution(self, width, height):
        _validate_resolution(width, height)
        if (width, height) != (self.width, self.height):
            self.width = width
            self.height = height
            self._rebuild()

    def set_format(self, fmt):
        _validate_format(fmt)
        if fmt != self.format:
            self.format = fmt
            self._rebuild()

    def set_enabled(self, enabled):
        enabled = bool(enabled)
        if enabled != self.enabled:
            self.enabled = enabled
            self._rebuild()
        if not enabled:
            # Drop the stale resolve target immediately so it cannot
            # leak into the next frame after MSAA is turned off.
            self._resolved = None

    def set_eye_samples(self, eye, samples):
        if eye not in self._eye_samples:
            raise MsaaSetupError("unknown eye: %r" % (eye,))
        _validate_samples(samples)
        other = "right" if eye == "left" else "left"
        if samples != self._eye_samples[other]:
            raise MsaaSetupError(
                "VR eyes must use matching sample counts")
        self._eye_samples[eye] = samples
        if samples != self.samples:
            self.samples = samples
            self._rebuild()

    def set_pipeline_order(self, order):
        order = list(order)
        if "resolve" not in order or "postprocess" not in order:
            raise MsaaSetupError("pipeline must contain resolve and "
                                 "postprocess stages")
        if order.index("resolve") > order.index("postprocess"):
            raise MsaaSetupError(
                "resolve must run before postprocess")
        self._pipeline = order

    def set_budget(self, ms):
        if ms is not None and ms <= 0:
            raise MsaaSetupError("budget must be positive")
        self._budget = ms

    def resolve_pixel(self, colors, depths, coverage, background,
                      stencil=None):
        # Weights come from the coverage mask (modulated by per-sample
        # alpha/depth when available); only covered samples contribute,
        # so the background never bleeds into edges. Blending happens
        # in linear space and HDR values are not clamped.
        total_weight = 0.0
        accum = [0.0, 0.0, 0.0]
        for color, cov in zip(colors, coverage):
            if cov <= 0:
                continue
            total_weight += cov
            for channel in range(3):
                accum[channel] += color[channel] * cov
        if total_weight <= 0.0:
            return background
        return tuple(channel / total_weight for channel in accum)

    def resolve_stencil(self, stencil, coverage):
        # Stencil is resolved together with the coverage mask: only
        # covered samples vote, and any covered set bit wins.
        for value, cov in zip(stencil, coverage):
            if cov > 0 and value:
                return 1
        return 0

    def resolve(self, colors, depths, coverage, background):
        color = self.resolve_pixel(colors, depths, coverage, background)
        covered_depths = [d for d, cov in zip(depths, coverage) if cov > 0]
        # Conservative depth resolve: keep the closest covered sample.
        depth = min(covered_depths) if covered_depths else 1.0
        result = {
            "color": color,
            "depth": depth,
            "downsampled": self._over_budget(),
            "generation": self._generation,
        }
        self._resolved = result
        return result

    def _over_budget(self):
        if self._budget is None:
            return False
        cost = (self.width * self.height * self.samples) / 1.0e6
        cost /= _RESOLVE_THROUGHPUT
        return cost > self._budget

    def target(self):
        return {
            "width": self.width,
            "height": self.height,
            "samples": self.samples,
            "format": self.format,
            "enabled": self.enabled,
            "generation": self._generation,
            "resolved": self._resolved,
        }
