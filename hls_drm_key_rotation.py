"""HLS DRM key rotation, device binding and download resume.

Coordinates three areas that must stay aligned across player versions:

* Key rotation: every rotation point is versioned per program and segment,
  optionally effective at a future server time.  Segments before and after a
  rotation point are validated with their own key and keys never leak across
  programs or ad breaks.
* Licenses and devices: a license binds at most ``max_devices`` devices.
  Overflow raises ``DeviceLimitExceeded`` instead of silently kicking an
  existing device, and unbinding releases the slot immediately.  Offline
  expiry is computed from server time only, so client clock changes cannot
  extend a license.
* Download resume: a task has at most one writer; a second acquirer receives
  ``download_in_progress``.  Progress is the contiguous segment range already
  written, so rewrites never roll progress back and reconnects resume at the
  correct segment.
"""

DEVICE_LIMIT_EXCEEDED = "device_limit_exceeded"
LICENSE_EXPIRED = "license_expired"
UNSUPPORTED_KEY_FORMAT = "unsupported_key_format"
DOWNLOAD_IN_PROGRESS = "download_in_progress"
RATE_LIMITED = "rate_limited"
KEY_NOT_FOUND = "key_not_found"
DOWNLOAD_NOT_ACQUIRED = "download_not_acquired"

SUPPORTED_KEY_FORMATS = frozenset((2,))


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
        self._devices = {}
        self._license_issued_at = {}
        self._rotations = {}
        self._ad_keys = {}
        self._request_times = []
        self._downloads = {}
        self._rotation_order = 0

    def advance(self, seconds):
        self.server_time += seconds
        return self.server_time

    def bind_license(self, license_id, device_id):
        bound = self._devices.setdefault(license_id, [])
        if license_id not in self._license_issued_at:
            self._license_issued_at[license_id] = self.server_time
        if device_id in bound:
            return
        if len(bound) >= self.max_devices:
            raise DeviceLimitExceeded(
                DEVICE_LIMIT_EXCEEDED,
                "license %s already binds %d devices"
                % (license_id, len(bound)))
        bound.append(device_id)

    def unbind_device(self, license_id, device_id):
        bound = self._devices.get(license_id)
        if bound and device_id in bound:
            bound.remove(device_id)

    def devices(self, license_id):
        return list(self._devices.get(license_id, ()))

    def rotate_key(self, program_id, segment_index, key_id, at=None):
        effective_at = self.server_time if at is None else at
        point = {
            "segment": segment_index,
            "key_id": key_id,
            "at": effective_at,
            "order": self._rotation_order,
        }
        self._rotation_order += 1
        self._rotations.setdefault(program_id, []).append(point)
        return point

    def key_for(self, program_id, segment_index):
        active = self._active_rotation_points(program_id, segment_index)
        if not active:
            raise DrmError(
                KEY_NOT_FOUND,
                "no key for program %s segment %d"
                % (program_id, segment_index))
        chosen = max(active, key=lambda point: (point["segment"],
                                                point["at"],
                                                point["order"]))
        return chosen["key_id"]

    def request_key(self, program_id, segment_index):
        cutoff = self.server_time - self.rate_window
        self._request_times = [
            timestamp for timestamp in self._request_times
            if timestamp > cutoff
        ]
        if len(self._request_times) >= self.rate_limit:
            retry_after = (
                self._request_times[0] + self.rate_window
                - self.server_time
            )
            if retry_after <= 0:
                retry_after = self.rate_window
            return {
                "status": "retry",
                "code": RATE_LIMITED,
                "retry_after": retry_after,
            }
        self._request_times.append(self.server_time)
        return {
            "status": "ok",
            "program_id": program_id,
            "segment": segment_index,
            "key_id": self.key_for(program_id, segment_index),
        }

    def validate_offline(self, license_id):
        issued_at = self._license_issued_at.get(license_id)
        if issued_at is None:
            return False
        return self.server_time < issued_at + self.license_ttl

    def acquire_download(self, task_id, device_id):
        task = self._downloads.get(task_id)
        if task is None:
            task = {"writer": None, "segments": set()}
            self._downloads[task_id] = task
        if task["writer"] is not None:
            return {
                "status": DOWNLOAD_IN_PROGRESS,
                "writer": task["writer"],
                "resume_at": self._progress(task),
            }
        task["writer"] = device_id
        return {
            "status": "acquired",
            "writer": device_id,
            "resume_at": self._progress(task),
        }

    def write_segment(self, task_id, segment_index):
        task = self._downloads.get(task_id)
        if task is None or task["writer"] is None:
            raise DrmError(
                DOWNLOAD_NOT_ACQUIRED,
                "task %s has no active writer" % task_id)
        if segment_index in task["segments"]:
            return {
                "status": "already_written",
                "segment": segment_index,
                "resume_at": self._progress(task),
            }
        task["segments"].add(segment_index)
        return {
            "status": "written",
            "segment": segment_index,
            "resume_at": self._progress(task),
        }

    def release_download(self, task_id):
        task = self._downloads.get(task_id)
        if task is not None:
            task["writer"] = None

    def resume_at(self, task_id):
        task = self._downloads.get(task_id)
        if task is None:
            return 0
        return self._progress(task)

    def supports_key_format(self, version):
        return version in SUPPORTED_KEY_FORMATS

    def check_key_format(self, version):
        if not self.supports_key_format(version):
            raise DrmError(
                UNSUPPORTED_KEY_FORMAT,
                "key format version %s is not supported by this player"
                % version)

    def rotate_ad_key(self, program_id, break_index, key_id):
        self._ad_keys.setdefault(program_id, {})[break_index] = key_id

    def ad_key_for(self, program_id, break_index):
        breaks = self._ad_keys.get(program_id)
        if breaks is None or break_index not in breaks:
            raise DrmError(
                KEY_NOT_FOUND,
                "no ad key for program %s break %d"
                % (program_id, break_index))
        return breaks[break_index]

    def audit(self, program_id):
        points = sorted(
            self._rotations.get(program_id, ()),
            key=lambda point: (point["segment"], point["at"],
                               point["order"]),
        )
        return [
            {"segment": point["segment"], "key_id": point["key_id"]}
            for point in points
        ]

    def _active_rotation_points(self, program_id, segment_index):
        return [
            point for point in self._rotations.get(program_id, ())
            if point["segment"] <= segment_index
            and point["at"] <= self.server_time
        ]

    @staticmethod
    def _progress(task):
        segments = task["segments"]
        progress = 0
        while progress in segments:
            progress += 1
        return progress
