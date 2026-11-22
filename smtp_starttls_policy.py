"""Per-recipient-domain SMTP STARTTLS policy and delivery gate (skeleton)."""


class SmtpPolicyError(Exception):
    def __init__(self, code, message=""):
        self.code = code
        super().__init__(code if not message else "%s: %s" % (code, message))


class StartTlsPolicy:
    def __init__(self, max_retries=5):
        self.max_retries = max_retries

    def set_domain_policy(self, domain, mode):
        raise NotImplementedError

    def get_policy(self, domain, loader=None):
        raise NotImplementedError

    def expire_policy(self, domain):
        raise NotImplementedError

    def open_connection(self, domain):
        raise NotImplementedError

    def send_message(self, connection_id, queue_id):
        raise NotImplementedError

    def queue_message(self, queue_id, domain, payload, policy_version=None):
        raise NotImplementedError

    def message_status(self, queue_id):
        raise NotImplementedError

    def prepare_payload(self, queue_id, *, downgrade=False):
        raise NotImplementedError

    def retry(self, queue_id):
        raise NotImplementedError

    def decide(self, domain, *, starttls, tlsa_status="valid", cert_hostname=None,
               mx_host=None, remote_code=None, queue_id=None):
        raise NotImplementedError

    def plan_delivery(self, domain, mx_records):
        raise NotImplementedError

    def decide_for_queued(self, queue_id):
        raise NotImplementedError

    def snapshot(self):
        raise NotImplementedError

    def restore(self, snapshot):
        raise NotImplementedError

    def audit_events(self):
        raise NotImplementedError
