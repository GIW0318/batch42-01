class MsaaSetupError(Exception):
    pass


class MsaaResolve:
    def __init__(self, samples=4, width=1920, height=1080,
                 format="RGBA16F"):
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

    def set_samples(self, samples):
        raise NotImplementedError()

    def set_resolution(self, width, height):
        raise NotImplementedError()

    def set_format(self, fmt):
        raise NotImplementedError()

    def set_enabled(self, enabled):
        raise NotImplementedError()

    def set_eye_samples(self, eye, samples):
        raise NotImplementedError()

    def set_pipeline_order(self, order):
        raise NotImplementedError()

    def set_budget(self, ms):
        raise NotImplementedError()

    def resolve_pixel(self, colors, depths, coverage, background,
                      stencil=None):
        raise NotImplementedError()

    def resolve_stencil(self, stencil, coverage):
        raise NotImplementedError()

    def resolve(self, colors, depths, coverage, background):
        raise NotImplementedError()

    def target(self):
        raise NotImplementedError()
