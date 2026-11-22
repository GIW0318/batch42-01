"""RSVP-TE LSP state, label and bandwidth planner."""

import copy
import threading

HOP_MISMATCH = "hop-mismatch"
NO_ROUTE = "no-route"
INSUFFICIENT_BANDWIDTH = "insufficient-bandwidth"
LABEL_IN_USE = "label-in-use"
RESOURCE_EXHAUSTED = "resource-exhausted"
LSP_EXISTS = "lsp-exists"
ADMIN_GROUP_MISMATCH = "admin-group-mismatch"
UNKNOWN_LSP = "unknown-lsp"
UNKNOWN_LINK = "unknown-link"


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
        self._links = {}
        self._adj = {}
        self._lsps = {}
        self._lock = threading.RLock()
        for link in links or ():
            if isinstance(link, dict):
                self.add_link(**link)
            else:
                self.add_link(*link)

    def add_link(self, src, dst, bandwidth, admin_group=0):
        with self._lock:
            key = (src, dst)
            if key not in self._links:
                self._adj.setdefault(src, []).append(dst)
            link = {"src": src, "dst": dst, "bandwidth": bandwidth,
                    "admin_group": admin_group, "reserved": 0}
            self._links[key] = link
            return link

    def _find_path(self, src, dst, visited=None):
        if src == dst:
            return [src]
        if visited is None:
            visited = set()
        visited.add(src)
        for nxt in self._adj.get(src, ()):
            if nxt in visited:
                continue
            segment = self._find_path(nxt, dst, visited)
            if segment is not None:
                return [src] + segment
        return None

    def _resolve_path(self, ero):
        if not ero:
            raise RsvpError(NO_ROUTE, "empty ERO")
        path = [ero[0]["node"]]
        prev_loose_unresolved = False
        for index in range(1, len(ero)):
            hop = ero[index]
            node = hop["node"]
            current = path[-1]
            if node == current:
                continue
            if hop.get("loose", False):
                segment = self._find_path(current, node)
                if segment is None:
                    prev_loose_unresolved = True
                    continue
                path.extend(segment[1:])
                prev_loose_unresolved = False
            else:
                direct = None if prev_loose_unresolved else \
                    self._links.get((current, node))
                if direct is not None:
                    path.append(node)
                else:
                    segment = self._find_path(current, node)
                    if segment is None:
                        raise RsvpError(
                            NO_ROUTE,
                            "no route from %s to %s" % (current, node))
                    path.extend(segment[1:])
                prev_loose_unresolved = False
        return path

    def _allocate_label(self, in_label):
        used = {record["label"] for record in self._lsps.values()}
        if in_label is not None:
            if in_label in used:
                raise RsvpError(LABEL_IN_USE,
                                "label %s already allocated" % in_label)
            if not self.label_min <= in_label <= self.label_max:
                raise RsvpError(
                    RESOURCE_EXHAUSTED,
                    "label %s outside [%s, %s]" % (in_label, self.label_min,
                                                 self.label_max))
            return in_label
        for label in range(self.label_min, self.label_max + 1):
            if label not in used:
                return label
        raise RsvpError(RESOURCE_EXHAUSTED, "label space exhausted")

    def _release(self, record):
        path = record["path"]
        for index in range(len(path) - 1):
            link = self._links.get((path[index], path[index + 1]))
            if link is not None:
                link["reserved"] -= record["bandwidth"]
        del self._lsps[record["id"]]

    @staticmethod
    def _public(record):
        return {
            "id": record["id"],
            "ero": copy.deepcopy(record["ero"]),
            "path": list(record["path"]),
            "bandwidth": record["bandwidth"],
            "priority": record["priority"],
            "admin_group": record["admin_group"],
            "label": record["label"],
            "deadline": record["deadline"],
        }

    def establish(self, lsp_id, ero, bandwidth, priority=7, admin_group=0,
                  in_label=None, resv_hops=None):
        with self._lock:
            if lsp_id in self._lsps:
                raise RsvpError(LSP_EXISTS,
                                "lsp %r already established" % lsp_id)
            path = self._resolve_path(ero)
            if resv_hops is not None and list(resv_hops) != path:
                raise RsvpError(
                    HOP_MISMATCH,
                    "resv hops %r do not match path %r" % (resv_hops, path))
            hop_links = []
            for index in range(len(path) - 1):
                link = self._links.get((path[index], path[index + 1]))
                if link is None:
                    raise RsvpError(
                        NO_ROUTE,
                        "no link %s->%s" % (path[index], path[index + 1]))
                hop_links.append(link)
            for link in hop_links:
                if link["admin_group"] & admin_group != admin_group:
                    raise RsvpError(
                        ADMIN_GROUP_MISMATCH,
                        "link %s->%s lacks admin group %#x" % (
                            link["src"], link["dst"], admin_group))
            for link in hop_links:
                if link["bandwidth"] - link["reserved"] < bandwidth:
                    raise RsvpError(
                        INSUFFICIENT_BANDWIDTH,
                        "link %s->%s has %s of %s needed" % (
                            link["src"], link["dst"],
                            link["bandwidth"] - link["reserved"], bandwidth))
            label = self._allocate_label(in_label)
            for link in hop_links:
                link["reserved"] += bandwidth
            record = {
                "id": lsp_id,
                "ero": copy.deepcopy(ero),
                "path": path,
                "bandwidth": bandwidth,
                "priority": priority,
                "admin_group": admin_group,
                "label": label,
                "deadline": self._clock + self.refresh_interval,
            }
            self._lsps[lsp_id] = record
            return self._public(record)

    def teardown(self, lsp_id):
        with self._lock:
            record = self._lsps.get(lsp_id)
            if record is None:
                raise RsvpError(UNKNOWN_LSP, "no such lsp %r" % lsp_id)
            self._release(record)
            return {
                "id": lsp_id,
                "released_label": record["label"],
                "downstream_notified": list(record["path"][1:]),
                "path": list(record["path"]),
            }

    def path_error(self, lsp_id, error_node):
        with self._lock:
            record = self._lsps.get(lsp_id)
            if record is None:
                return False
            self._release(record)
            return True

    def lsp(self, lsp_id):
        with self._lock:
            record = self._lsps.get(lsp_id)
            return None if record is None else self._public(record)

    def lsp_count(self):
        with self._lock:
            return len(self._lsps)

    def available(self, src, dst):
        with self._lock:
            link = self._links.get((src, dst))
            if link is None:
                raise RsvpError(UNKNOWN_LINK, "no link %s->%s" % (src, dst))
            return link["bandwidth"] - link["reserved"]

    def refresh(self, lsp_id):
        with self._lock:
            record = self._lsps.get(lsp_id)
            if record is None:
                raise RsvpError(UNKNOWN_LSP, "no such lsp %r" % lsp_id)
            record["deadline"] = self._clock + self.refresh_interval
            return record["deadline"]

    def refresh_deadline(self, lsp_id):
        with self._lock:
            record = self._lsps.get(lsp_id)
            return None if record is None else record["deadline"]

    def advance(self, seconds):
        with self._lock:
            self._clock += seconds
            return self._clock

    def expired(self):
        with self._lock:
            doomed = [record for record in self._lsps.values()
                      if record["deadline"] <= self._clock]
            doomed.sort(key=lambda record: (record["deadline"], record["id"]))
            return [record["id"] for record in doomed]

    def preemption_order(self):
        with self._lock:
            ordered = sorted(self._lsps.values(),
                             key=lambda record: (record["priority"],
                                                 record["id"]))
            return [record["id"] for record in ordered]

    def reconcile(self, lsp_id, ero):
        with self._lock:
            record = self._lsps.get(lsp_id)
            if record is None:
                return False
            try:
                path = self._resolve_path(ero)
            except RsvpError:
                return False
            return path == record["path"]

    def snapshot(self):
        with self._lock:
            return copy.deepcopy({
                "label_min": self.label_min,
                "label_max": self.label_max,
                "refresh_interval": self.refresh_interval,
                "clock": self._clock,
                "links": list(self._links.values()),
                "lsps": list(self._lsps.values()),
            })

    def restore(self, snapshot):
        with self._lock:
            self.label_min = snapshot.get("label_min", self.label_min)
            self.label_max = snapshot.get("label_max", self.label_max)
            self.refresh_interval = snapshot.get("refresh_interval",
                                                 self.refresh_interval)
            self._clock = snapshot.get("clock", 0)
            self._links = {}
            self._adj = {}
            self._lsps = {}
            for link in snapshot.get("links", []):
                self.add_link(link["src"], link["dst"], link["bandwidth"],
                              link.get("admin_group", 0))
                self._links[(link["src"], link["dst"])]["reserved"] = \
                    link.get("reserved", 0)
            for record in snapshot.get("lsps", []):
                self._lsps[record["id"]] = copy.deepcopy(record)
