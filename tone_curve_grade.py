class GradeConfigError(Exception):
    pass


class ToneGrade:
    def __init__(self, lut_size=16, exposure_time_ms=300.0, gamma=2.2):
        self.lut_size = lut_size
        self.exposure_time_ms = exposure_time_ms
        self.gamma = gamma
        self._lut = {}
        self._exposure = 1.0
        self._scene_id = None
        self._scene_metering = 0.5

    def set_lut(self, entries, size=None):
        raise NotImplementedError()

    def sample_lut(self, color):
        raise NotImplementedError()

    def auto_exposure(self, scene_luminance, dt_ms):
        raise NotImplementedError()

    def exposure(self):
        raise NotImplementedError()

    def grade(self, color, exposure=1.0, ui=False):
        raise NotImplementedError()

    def tonemap(self, color):
        raise NotImplementedError()

    def hdr_to_sdr(self, color):
        raise NotImplementedError()

    def set_scene(self, scene_id, lut=None, metering=0.5):
        raise NotImplementedError()

    def end_cutscene(self):
        raise NotImplementedError()

    def composite_layers(self, layers):
        raise NotImplementedError()
