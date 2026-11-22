"""MFA enrollment ledger keyed by user, device and one-time code (skeleton)."""


class MfaError(Exception):
    def __init__(self, code, message=""):
        self.code = code
        super().__init__(code if not message else "%s: %s" % (code, message))


class MfaEnrollmentLedger:
    def __init__(self, enforcement=True, totp_step=30, totp_window=1,
                 grace_days=7, max_confirm_attempts=5):
        self.enforcement = enforcement
        self.totp_step = totp_step
        self.totp_window = totp_window
        self.grace_days = grace_days
        self.max_confirm_attempts = max_confirm_attempts

    def activate_user(self, user, *, now=0.0):
        raise NotImplementedError

    def begin_enrollment(self, user, device_id, *, actor=None):
        raise NotImplementedError

    def confirm_enrollment(self, user, device_id, code, *, now=0.0, actor=None):
        raise NotImplementedError

    def enrollments(self, user):
        raise NotImplementedError

    def verify(self, user, code, *, device_id=None, now=0.0):
        raise NotImplementedError

    def remove_device(self, user, device_id, *, actor=None):
        raise NotImplementedError

    def generate_backup_codes(self, user, *, actor=None):
        raise NotImplementedError

    def use_backup_code(self, user, code):
        raise NotImplementedError

    def set_phone(self, user, phone):
        raise NotImplementedError

    def change_phone(self, user, phone):
        raise NotImplementedError

    def backup_phone(self, user):
        raise NotImplementedError

    def can_send_to(self, user, phone):
        raise NotImplementedError

    def recover(self, actor, user, *, id_number, mfa_ok=False):
        raise NotImplementedError

    def grace_active(self, user, now=0.0):
        raise NotImplementedError

    def reset_grace(self, user):
        raise NotImplementedError

    def snapshot(self):
        raise NotImplementedError

    def restore(self, snapshot):
        raise NotImplementedError

    def audit_events(self):
        raise NotImplementedError
