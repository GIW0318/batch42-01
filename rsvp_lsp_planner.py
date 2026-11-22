"""RSVP-TE LSP state, label and bandwidth planner skeleton."""

HOP_MISMATCH = "hop-mismatch"
NO_ROUTE = "no-route"
INSUFFICIENT_BANDWIDTH = "insufficient-bandwidth"
LABEL_IN_USE = "label-in-use"
RESOURCE_EXHAUSTED = "resource-exhausted"
LSP_EXISTS = "lsp-exists"
ADMIN_GROUP_MISMATCH = "admin-group-mismatch"


class RsvpError(Exception):
    def __init__(self, code, message=""):
        self.code = code
        super().__init__(code if not message else "%s: %s" % (code, message))


class RsvpLspPlanner:
    """Atomic LSP establishment with ERO, label and bandwidth accounting.

    ERO hops are dicts {"node": "A", "loose": False}; loose hops may match a
    multi-hop path, strict hops must be direct links.  Lower priority numbers
    are more important.  All reservations for one LSP commit together.
    """

    def __init__(self, links=None, label_min=16, label_max=1000,
                 refresh_interval=30):
        self.label_min = label_min
        self.label_max = label_max
        self.refresh_interval = refresh_interval
        self._clock = 0

    def add_link(self, src, dst, bandwidth, admin_group=0):
        raise NotImplementedError

    def establish(self, lsp_id, ero, bandwidth, priority=7, admin_group=0,
                  in_label=None, resv_hops=None):
        raise NotImplementedError

    def teardown(self, lsp_id):
        raise NotImplementedError

    def path_error(self, lsp_id, error_node):
        raise NotImplementedError

    def lsp(self, lsp_id):
        raise NotImplementedError

    def lsp_count(self):
        raise NotImplementedError

    def available(self, src, dst):
        raise NotImplementedError

    def refresh(self, lsp_id):
        raise NotImplementedError

    def refresh_deadline(self, lsp_id):
        raise NotImplementedError

    def advance(self, seconds):
        raise NotImplementedError

    def expired(self):
        raise NotImplementedError

    def preemption_order(self):
        raise NotImplementedError

    def reconcile(self, lsp_id, ero):
        raise NotImplementedError

    def snapshot(self):
        raise NotImplementedError

    def restore(self, snapshot):
        raise NotImplementedError
