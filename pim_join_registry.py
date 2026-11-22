"""PIM (*,G)/(S,G) join registry skeleton."""

NOT_DIRECTLY_CONNECTED = "not-directly-connected"
TABLE_FULL = "table-full"
SOURCE_REQUIRED = "source-required"
BAD_MODE = "bad-mode"
BAD_SOURCE_COUNT = "bad-source-count"
BAD_GROUP = "bad-group"
BAD_HOLDTIME = "bad-holdtime"
BAD_ADDRESS = "bad-address"

ASM = "asm"
SSM = "ssm"
HEADER_LEN = 6
IPV4_LEN = 4


class PimError(Exception):
    def __init__(self, code, message=""):
        self.code = code
        super().__init__(code if not message else "%s: %s" % (code, message))


class PimParseError(PimError):
    def __init__(self, code, field):
        self.field = field
        super().__init__(code, "field=%s" % field)


def _split_ipv4(address):
    parts = str(address).split(".")
    if len(parts) != IPV4_LEN:
        raise PimError(BAD_ADDRESS, address)
    try:
        octets = tuple(int(part) for part in parts)
    except ValueError:
        raise PimError(BAD_ADDRESS, address)
    if any(octet < 0 or octet > 255 for octet in octets):
        raise PimError(BAD_ADDRESS, address)
    return octets


def _format_ipv4(octets):
    return "%d.%d.%d.%d" % tuple(octets)


def _is_ssm_range(group):
    return _split_ipv4(group)[0] == 232


class _Entry:
    __slots__ = ("group", "source", "mode", "interfaces", "upstream", "holdtime")

    def __init__(self, group, source, mode, holdtime, upstream=None):
        self.group = group
        self.source = source
        self.mode = mode
        self.interfaces = {}
        self.upstream = upstream
        self.holdtime = holdtime


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
        self._entries = {}
        self._registering = set()
        self._rejoin_queue = []

    def parse_join_prune(self, payload):
        if len(payload) < IPV4_LEN:
            raise PimParseError(BAD_GROUP, "group")
        group = _format_ipv4(payload[0:IPV4_LEN])
        if len(payload) < IPV4_LEN + 1:
            raise PimParseError(BAD_SOURCE_COUNT, "source_count")
        source_count = payload[4]
        if len(payload) < HEADER_LEN:
            raise PimParseError(BAD_HOLDTIME, "holdtime")
        holdtime = payload[5]
        needed = HEADER_LEN + source_count * IPV4_LEN
        if len(payload) < needed:
            raise PimParseError(BAD_SOURCE_COUNT, "source_count")
        sources = []
        offset = HEADER_LEN
        for _ in range(source_count):
            sources.append(_format_ipv4(payload[offset:offset + IPV4_LEN]))
            offset += IPV4_LEN
        return {"group": group, "holdtime": holdtime, "sources": sources}

    def add_join(self, group, source=None, interface="eth0", holdtime=None, mode="asm"):
        if mode not in (ASM, SSM):
            raise PimError(BAD_MODE, mode)
        _split_ipv4(group)
        if source is not None:
            _split_ipv4(source)
        if _is_ssm_range(group):
            mode = SSM
        if mode == SSM and source is None:
            raise PimError(SOURCE_REQUIRED, group)

        key = (group, source)
        entry = self._entries.get(key)
        if entry is None and len(self._entries) >= self.max_entries:
            raise PimError(TABLE_FULL, group)

        effective_holdtime = self.holdtime_default if holdtime is None else holdtime
        new_interfaces = dict(entry.interfaces) if entry is not None else {}
        new_interfaces[interface] = self._clock + effective_holdtime

        if entry is None:
            entry = _Entry(group, source, mode, effective_holdtime)
            self._entries[key] = entry
        entry.interfaces = new_interfaces
        entry.mode = mode
        entry.holdtime = effective_holdtime
        return True

    def prune_join(self, group, source=None, interface="eth0"):
        return self._remove_interface(group, source, interface)

    def outgoing(self, group, source=None):
        entry = self._entries.get((group, source))
        return list(entry.interfaces) if entry is not None else []

    def upstream(self, group, source=None):
        entry = self._entries.get((group, source))
        if entry is None:
            entry = self._entries.get((group, None))
        return entry.upstream if entry is not None else None

    def group_view(self, group):
        star_entry = self._entries.get((group, None))
        star = None
        if star_entry is not None:
            star = {
                "interfaces": list(star_entry.interfaces),
                "mode": star_entry.mode,
            }
        sources = {}
        for (entry_group, entry_source), entry in self._entries.items():
            if entry_group == group and entry_source is not None:
                sources[entry_source] = {
                    "interfaces": list(entry.interfaces),
                    "mode": entry.mode,
                }
        return {"group": group, "star": star, "sources": sources}

    def entry_count(self):
        return len(self._entries)

    def advance(self, seconds):
        self._clock += seconds
        return self._clock

    def expire(self):
        expired = []
        for key in list(self._entries):
            entry = self._entries[key]
            stale = [
                interface
                for interface, deadline in entry.interfaces.items()
                if deadline <= self._clock
            ]
            if not stale:
                continue
            remaining = dict(entry.interfaces)
            for interface in stale:
                del remaining[interface]
            entry.interfaces = remaining
            if not remaining:
                expired.append({
                    "group": entry.group,
                    "source": entry.source,
                    "mode": entry.mode,
                })
                del self._entries[key]
        return expired

    def mode_of(self, group, source=None):
        entry = self._entries.get((group, source))
        return entry.mode if entry is not None else None

    def elect_rp(self, candidates):
        if not candidates:
            return None
        winner = min(
            candidates,
            key=lambda candidate: (
                candidate["priority"],
                -candidate["hash"],
                candidate["name"],
            ),
        )
        return winner["name"]

    def register_source(self, group, source, directly_connected=True):
        _split_ipv4(group)
        _split_ipv4(source)
        if not directly_connected:
            raise PimError(NOT_DIRECTLY_CONNECTED, source)
        if self.local_sources and source not in self.local_sources:
            raise PimError(NOT_DIRECTLY_CONNECTED, source)
        self._registering.add((group, source))
        return True

    def register_stop(self, group, source):
        self._registering.discard((group, source))
        return True

    def is_registering(self, group, source):
        return (group, source) in self._registering

    def assert_lost(self, group, source, interface):
        return self._remove_interface(group, source, interface)

    def rejoin_list(self):
        pending = self._rejoin_queue
        self._rejoin_queue = []
        return pending

    def snapshot(self):
        return {
            "version": 1,
            "clock": self._clock,
            "entries": [
                {
                    "group": entry.group,
                    "source": entry.source,
                    "mode": entry.mode,
                    "upstream": entry.upstream,
                    "interfaces": list(entry.interfaces),
                    "expires": {
                        interface: deadline - self._clock
                        for interface, deadline in entry.interfaces.items()
                    },
                }
                for entry in self._entries.values()
            ],
            "registering": [list(key) for key in sorted(self._registering)],
        }

    def restore(self, snapshot):
        self._entries = {}
        self._registering = set()
        self._rejoin_queue = []
        self._clock = snapshot.get("clock", 0)
        for item in snapshot.get("entries", ()):
            group = item["group"]
            source = item.get("source")
            mode = item.get("mode", ASM)
            interfaces = {}
            for interface in item.get("interfaces", ()):
                remaining = item.get("expires", {}).get(
                    interface, self.holdtime_default
                )
                interfaces[interface] = self._clock + remaining
            entry = _Entry(group, source, mode, self.holdtime_default,
                           upstream=item.get("upstream"))
            entry.interfaces = interfaces
            self._entries[(group, source)] = entry
            self._rejoin_queue.append({
                "group": group,
                "source": source,
                "mode": mode,
                "interfaces": list(interfaces),
            })
        for group, source in snapshot.get("registering", ()):
            self._registering.add((group, source))
        return len(self._entries)

    def _remove_interface(self, group, source, interface):
        entry = self._entries.get((group, source))
        if entry is None or interface not in entry.interfaces:
            return False
        remaining = dict(entry.interfaces)
        del remaining[interface]
        entry.interfaces = remaining
        if not remaining:
            del self._entries[(group, source)]
        return True
