"""HLS DRM key rotation, device binding and download resume (skeleton).

A license binds a fixed number of devices, segment keys are versioned by
rotation point, offline expiry uses server time and only one writer owns a
download task.  No behaviour is implemented yet.
"""

DEVICE_LIMIT_EXCEEDED = "device_limit_exceeded"
LICENSE_EXPIRED = "license_expired"
UNSUPPORTED_KEY_FORMAT = "unsupported_key_format"
DOWNLOAD_IN_PROGRESS = "download_in_progress"
RATE_LIMITED = "rate_limited"


class DrmError(Exception):
    def __init__(self, code, message=""):
        self.code = code
        super().__init__(code if not message else "%s: %s" % (code, message))


class DeviceLimitExceeded(DrmError):
    pass


class HlsDrmKeyRotation:
    def __init__(self, max_devices=4, server_time=0, license_ttl=86400,
                 rate_limit=3, rate_window=1.0):
        self.max_devices = max_devices
        self.server_time = server_time
        self.license_ttl = license_ttl
        self.rate_limit = rate_limit
        self.rate_window = rate_window

    def advance(self, seconds):
        raise NotImplementedError

    def bind_license(self, license_id, device_id):
        raise NotImplementedError

    def unbind_device(self, license_id, device_id):
        raise NotImplementedError

    def devices(self, license_id):
        raise NotImplementedError

    def rotate_key(self, program_id, segment_index, key_id, at=None):
        raise NotImplementedError

    def key_for(self, program_id, segment_index):
        raise NotImplementedError

    def request_key(self, program_id, segment_index):
        raise NotImplementedError

    def validate_offline(self, license_id):
        raise NotImplementedError

    def acquire_download(self, task_id, device_id):
        raise NotImplementedError

    def write_segment(self, task_id, segment_index):
        raise NotImplementedError

    def release_download(self, task_id):
        raise NotImplementedError

    def resume_at(self, task_id):
        raise NotImplementedError

    def supports_key_format(self, version):
        raise NotImplementedError

    def check_key_format(self, version):
        raise NotImplementedError

    def rotate_ad_key(self, program_id, break_index, key_id):
        raise NotImplementedError

    def ad_key_for(self, program_id, break_index):
        raise NotImplementedError

    def audit(self, program_id):
        raise NotImplementedError
