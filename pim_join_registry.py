"""PIM (*,G)/(S,G) join registry skeleton."""

NOT_DIRECTLY_CONNECTED = "not-directly-connected"
TABLE_FULL = "table-full"
SOURCE_REQUIRED = "source-required"
BAD_MODE = "bad-mode"
BAD_SOURCE_COUNT = "bad-source-count"


class PimError(Exception):
    def __init__(self, code, message=""):
        self.code = code
        super().__init__(code if not message else "%s: %s" % (code, message))


class PimParseError(PimError):
    def __init__(self, code, field):
        self.field = field
        super().__init__(code, "field=%s" % field)


class PimJoinRegistry:
    """Join/Prune table keyed by (group, source) with (*,G) association.

    The wire format accepted by parse_join_prune is four group bytes, one
    source-count byte, one holdtime byte and that many four-byte sources.
    Updates and outgoing-interface changes are all-or-nothing.
    """

    def __init__(self, max_entries=1000, local_sources=(), holdtime_default=180):
        self.max_entries = max_entries
        self.local_sources = tuple(local_sources)
        self.holdtime_default = holdtime_default
        self._clock = 0

    def parse_join_prune(self, payload):
        raise NotImplementedError

    def add_join(self, group, source=None, interface="eth0", holdtime=None, mode="asm"):
        raise NotImplementedError

    def prune_join(self, group, source=None, interface="eth0"):
        raise NotImplementedError

    def outgoing(self, group, source=None):
        raise NotImplementedError

    def upstream(self, group, source=None):
        raise NotImplementedError

    def group_view(self, group):
        raise NotImplementedError

    def entry_count(self):
        raise NotImplementedError

    def advance(self, seconds):
        raise NotImplementedError

    def expire(self):
        raise NotImplementedError

    def mode_of(self, group, source=None):
        raise NotImplementedError

    def elect_rp(self, candidates):
        raise NotImplementedError

    def register_source(self, group, source, directly_connected=True):
        raise NotImplementedError

    def register_stop(self, group, source):
        raise NotImplementedError

    def is_registering(self, group, source):
        raise NotImplementedError

    def assert_lost(self, group, source, interface):
        raise NotImplementedError

    def rejoin_list(self):
        raise NotImplementedError

    def snapshot(self):
        raise NotImplementedError

    def restore(self, snapshot):
        raise NotImplementedError
