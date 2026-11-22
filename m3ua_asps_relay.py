"""M3UA ASP state and routing relay skeleton."""

DUPLICATE_POINT_CODE = "duplicate-point-code"
NETWORK_APPEARANCE_MISMATCH = "network-appearance-mismatch"
ROUTING_CONTEXT_MISMATCH = "routing-context-mismatch"
STALE_SEQUENCE = "stale-sequence"
DUPU_OVERRUN = "dupu-overrun"
CONGESTED = "congested"
UNSUPPORTED_SI = "unsupported-si"
QUEUE_FULL = "queue-full"
UNKNOWN_ASP = "unknown-asp"
AS_INACTIVE = "as-inactive"


class M3uaError(Exception):
    def __init__(self, code, message="", field=None):
        self.code = code
        self.field = field
        super().__init__(code if not message else "%s: %s" % (code, message))


class M3uaAspsRelay:
    """ASP sets isolated by Routing Context with idempotent state changes.

    Protocol-data sequence comparison treats values as unsigned 32-bit
    numbers with wrap-around.  The send queue reserves a minimum share for
    low-priority traffic.
    """

    def __init__(self, queue_limit=100, low_priority_quota=2):
        self.queue_limit = queue_limit
        self.low_priority_quota = low_priority_quota

    def add_as(self, as_name, routing_context, point_codes=(), mode="override",
               network_appearance=None, service_indicators=(3,)):
        raise NotImplementedError

    def add_asp(self, asp_id, as_name, network_appearance=None):
        raise NotImplementedError

    def asp_active(self, asp_id, seq, routing_context=None):
        raise NotImplementedError

    def asp_inactive(self, asp_id, seq):
        raise NotImplementedError

    def active_asp(self, as_name):
        raise NotImplementedError

    def parse_aspm(self, data):
        raise NotImplementedError

    def send_data(self, as_name, payload, priority=0, si=3):
        raise NotImplementedError

    def next_message(self):
        raise NotImplementedError

    def queue_size(self):
        raise NotImplementedError

    def heartbeat_timeout(self, asp_id):
        raise NotImplementedError

    def handle_dupu(self, data):
        raise NotImplementedError

    def sequence_in_order(self, previous, current):
        raise NotImplementedError

    def route_table(self):
        raise NotImplementedError

    def snapshot(self):
        raise NotImplementedError

    def restore(self, snapshot):
        raise NotImplementedError
