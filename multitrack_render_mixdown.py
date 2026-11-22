"""Multitrack stem rendering, latency compensation and export (skeleton).

Stems sum to the master within tolerance, mute and solo are frozen at export
start, plugin delay is compensated per sample and export is atomic.  No
behaviour is implemented yet.
"""

EXPORT_FAILED = "export_failed"
UNKNOWN_TRACK = "unknown_track"
UNKNOWN_EXPORT = "unknown_export"


class MixdownError(Exception):
    def __init__(self, code, message=""):
        self.code = code
        super().__init__(code if not message else "%s: %s" % (code, message))


class MultitrackRenderMixdown:
    def __init__(self, sample_rate=48000, block_size=512):
        self.sample_rate = sample_rate
        self.block_size = block_size

    def add_track(self, track_id, samples=None, name=None):
        raise NotImplementedError

    def set_track_gain(self, track_id, db):
        raise NotImplementedError

    def set_mute(self, track_id, muted):
        raise NotImplementedError

    def set_solo(self, track_id, solo):
        raise NotImplementedError

    def set_plugin_delay(self, track_id, samples):
        raise NotImplementedError

    def set_automation(self, track_id, points):
        raise NotImplementedError

    def add_clip(self, track_id, start, length, fade_in=0, fade_out=0):
        raise NotImplementedError

    def render_stem(self, track_id):
        raise NotImplementedError

    def render_master(self):
        raise NotImplementedError

    def begin_export(self):
        raise NotImplementedError

    def complete_stem(self, tx, track_id):
        raise NotImplementedError

    def commit_export(self, tx):
        raise NotImplementedError

    def export(self, path, sample_rate=None, fail_disk=False):
        raise NotImplementedError

    def export_stems(self, directory):
        raise NotImplementedError

    def export_status(self):
        raise NotImplementedError

    def recover_export(self, tx):
        raise NotImplementedError

    def set_limiter(self, enabled):
        raise NotImplementedError

    def peak(self):
        raise NotImplementedError
