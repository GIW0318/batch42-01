import copy
import hashlib
import heapq
import logging
import math
import threading
from collections import OrderedDict


logger = logging.getLogger(__name__)

_EPS = 1e-9


class NavMeshQueryError(Exception):
    def __init__(self, message, cell=None):
        super().__init__(message)
        self.cell = cell


class CorridorFollow:
    def __init__(self, cell_size=1.0, max_replans_per_frame=8):
        self.cell_size = float(cell_size)
        self.max_replans_per_frame = int(max_replans_per_frame)
        self.version = 0
        self._revision = 0
        self._polygons = OrderedDict()
        self._walkable = OrderedDict()
        self._links = OrderedDict()
        self._agents = {}
        self._queue = OrderedDict()
        self._pending_version = None
        self._pending_polygons = None
        self._pending_links = None
        self._lock = threading.RLock()

    # ------------------------------------------------------------------
    # Navmesh lifecycle and queries
    # ------------------------------------------------------------------
    def load_navmesh(self, version, polygons, links=None):
        with self._lock:
            polygons = self._normalise_polygons(polygons)
            self._polygons = polygons
            self._walkable = OrderedDict((pid, True) for pid in polygons)
            self._links = self._normalise_links(links)
            self.version = version
            self._revision = self._mesh_fingerprint()
            self._pending_version = None
            self._pending_polygons = None
            self._pending_links = None
            self._agents.clear()
            self._queue.clear()

    def set_walkable(self, polygon_id, walkable):
        with self._lock:
            if polygon_id not in self._polygons:
                raise KeyError(polygon_id)
            walkable = bool(walkable)
            if self._walkable.get(polygon_id) == walkable:
                return
            self._walkable[polygon_id] = walkable
            self._mesh_changed(bump_version=True)

    def polygon_at(self, point):
        with self._lock:
            return self._walkable_polygon_at(point)

    def add_link(self, link_id, from_polygon, to_polygon, one_way=True):
        with self._lock:
            if from_polygon not in self._polygons:
                raise KeyError(from_polygon)
            if to_polygon not in self._polygons:
                raise KeyError(to_polygon)
            self._links[link_id] = {
                "from_polygon": from_polygon,
                "to_polygon": to_polygon,
                "one_way": bool(one_way),
            }
            self._mesh_changed(bump_version=True)

    def find_path(self, start, goal):
        with self._lock:
            start = self._as_point(start)
            goal = self._as_point(goal)
            start_polygon = self._require_walkable_polygon(start, "invalid path start")
            goal_polygon = self._require_walkable_polygon(goal, "invalid path goal")

            if not self._walkable or not any(self._walkable.values()):
                self._query_failure(goal, "navmesh has no walkable polygons")

            polygon_ids, traversals = self._search(start_polygon, goal_polygon)
            if polygon_ids is None:
                self._query_failure(goal, "goal is unreachable from start")

            points = [start]
            for traversal in traversals:
                if traversal["kind"] == "portal":
                    self._append_point(points, traversal["portal"])
                else:
                    self._append_point(points, traversal["from_point"])
                    self._append_point(points, traversal["to_point"])
            self._append_point(points, goal)
            if len(points) == 1:
                points.append(goal)
            elif self._distance(points[-1], goal) <= _EPS:
                points[-1] = goal

            return {
                "version": self.version,
                "_revision": self._revision,
                "start": start,
                "goal": goal,
                "points": points,
                "polygons": list(polygon_ids),
            }

    # ------------------------------------------------------------------
    # Following and local avoidance
    # ------------------------------------------------------------------
    def follow(self, agent_id, position, path=None):
        with self._lock:
            position = self._as_point(position)
            state = self._agents.get(agent_id)
            same_path = False

            if path is None:
                if state and state.get("path") is not None:
                    path = state["path"]
                    same_path = True
                elif state and state.get("goal") is not None:
                    path = self.find_path(self._snap_to_mesh(position), state["goal"])
                else:
                    self._query_failure(position, "agent has no path or goal")
            else:
                path = self._normalise_path(path)
                self._validate_path(path, position)
                if state and state.get("path") is not None:
                    same_path = self._paths_equivalent(path, state["path"])
                if not same_path:
                    path = copy.deepcopy(path)

            self._validate_path(path, position)
            snapped = self._snap_to_mesh(position)
            points = path["points"]
            goal = path["goal"]
            tolerance = max(1e-6, self.cell_size * 0.1)

            start_index = state.get("index", 0) if same_path and state else 0
            arrived = self._distance(snapped, goal) <= tolerance
            if arrived:
                index = len(points) - 1
            else:
                index = self._advance_index(points, snapped, start_index)

            replanned = False
            if not arrived:
                route_distance = self._distance_to_polyline(snapped, points, 0)
                teleport_threshold = max(self.cell_size * 4.0, 1e-6)
                route_polygons = set(path.get("polygons", []))
                position_polygon = self._walkable_polygon_at(snapped)
                left_route_region = (
                    not route_polygons or position_polygon not in route_polygons
                )
                if left_route_region and route_distance > teleport_threshold:
                    path = self.find_path(snapped, goal)
                    self._validate_path(path, snapped)
                    points = path["points"]
                    index = self._advance_index(points, snapped, 0)
                    arrived = self._distance(snapped, path["goal"]) <= tolerance
                    if arrived:
                        index = len(points) - 1
                    replanned = True

            self._agents[agent_id] = {
                "position": snapped,
                "goal": path["goal"],
                "path": copy.deepcopy(path),
                "index": index,
            }
            return {
                "agent_id": agent_id,
                "point": snapped,
                "position": snapped,
                "goal": path["goal"],
                "index": index,
                "arrived": bool(arrived),
                "version": path["version"],
                "path": copy.deepcopy(path),
                "replanned": replanned,
            }

    def apply_avoidance(self, agent_id, position, offset):
        with self._lock:
            position = self._as_point(position)
            offset = self._as_point(offset)
            current = self._snap_to_mesh(position)
            candidate = (current[0] + offset[0], current[1] + offset[1])

            if self._walkable_polygon_at(candidate) is not None:
                resolved = candidate
            else:
                current_polygon = self._walkable_polygon_at(current)
                if current_polygon is not None:
                    resolved = self._nearest_point_on_polygon(
                        candidate, self._polygons[current_polygon]
                    )
                else:
                    resolved = self._nearest_walkable_point(candidate)
                resolved = self._snap_to_mesh(resolved)

            state = self._agents.get(agent_id)
            if state is not None:
                state["position"] = resolved
            return resolved

    # ------------------------------------------------------------------
    # Replanning queue
    # ------------------------------------------------------------------
    def request_replan(self, agent_id, goal, position=None):
        with self._lock:
            if agent_id in self._queue:
                return False
            request = {"goal": self._as_point(goal)}
            if position is not None:
                request["position"] = self._as_point(position)
            self._queue[agent_id] = request
            return True

    def process_replans(self):
        with self._lock:
            limit = max(0, self.max_replans_per_frame)
            agent_ids = list(self._queue.keys())[:limit]
            processed = []

            for agent_id in agent_ids:
                request = self._queue[agent_id]
                state = self._agents.get(agent_id)
                goal = request["goal"]

                if "position" in request:
                    start = request["position"]
                elif state and state.get("position") is not None:
                    start = state["position"]
                elif state and state.get("path"):
                    start = state["path"]["points"][0]
                else:
                    start = goal

                path = self.find_path(start, goal)
                index = self._advance_index(path["points"], start, 0)
                tolerance = max(1e-6, self.cell_size * 0.1)
                arrived = self._distance(start, goal) <= tolerance
                if arrived:
                    index = len(path["points"]) - 1

                self._agents[agent_id] = {
                    "position": start,
                    "goal": goal,
                    "path": copy.deepcopy(path),
                    "index": index,
                }
                processed.append(
                    {
                        "agent_id": agent_id,
                        "path": copy.deepcopy(path),
                        "version": path["version"],
                        "arrived": bool(arrived),
                    }
                )
                del self._queue[agent_id]

            return processed

    # ------------------------------------------------------------------
    # Asynchronous rebuild
    # ------------------------------------------------------------------
    def begin_rebuild(self, next_version, polygons=None, links=None):
        with self._lock:
            self._pending_version = next_version
            self._pending_polygons = (
                self._normalise_polygons(polygons) if polygons is not None else None
            )
            self._pending_links = (
                self._normalise_links(links) if links is not None else None
            )

    def commit_rebuild(self, polygons=None, links=None):
        with self._lock:
            if polygons is None:
                if self._pending_polygons is None:
                    self._query_failure(
                        (0, 0), "commit_rebuild called without replacement polygons"
                    )
                polygons = copy.deepcopy(self._pending_polygons)
            else:
                polygons = self._normalise_polygons(polygons)

            if links is not None:
                new_links = self._normalise_links(links)
            elif self._pending_links is not None:
                new_links = copy.deepcopy(self._pending_links)
            else:
                new_links = OrderedDict(
                    (
                        link_id,
                        copy.deepcopy(link),
                    )
                    for link_id, link in self._links.items()
                    if link["from_polygon"] in polygons
                    and link["to_polygon"] in polygons
                )

            old_walkable = self._walkable
            self._polygons = polygons
            self._walkable = OrderedDict(
                (pid, old_walkable.get(pid, True)) for pid in polygons
            )
            self._links = new_links
            if self._pending_version is None:
                self._bump_version()
            else:
                self.version = self._pending_version
            self._revision = self._mesh_fingerprint()
            self._pending_version = None
            self._pending_polygons = None
            self._pending_links = None

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------
    def save_state(self):
        with self._lock:
            agents = []
            for agent_id, state in self._agents.items():
                agents.append(
                    {
                        "agent_id": agent_id,
                        "position": self._point_to_list(state.get("position")),
                        "goal": self._point_to_list(state.get("goal")),
                        "path": self._path_to_state(state.get("path")),
                        "index": state.get("index", 0),
                    }
                )

            queue = []
            for agent_id, request in self._queue.items():
                queue.append(
                    {
                        "agent_id": agent_id,
                        "goal": self._point_to_list(request["goal"]),
                        "position": self._point_to_list(request.get("position")),
                    }
                )

            return {
                "version": self.version,
                "revision": self._revision,
                "cell_size": self.cell_size,
                "max_replans_per_frame": self.max_replans_per_frame,
                "polygons": [
                    [pid, [self._point_to_list(point) for point in vertices]]
                    for pid, vertices in self._polygons.items()
                ],
                "walkable": [
                    [pid, walkable] for pid, walkable in self._walkable.items()
                ],
                "links": [
                    [
                        link_id,
                        link["from_polygon"],
                        link["to_polygon"],
                        link["one_way"],
                    ]
                    for link_id, link in self._links.items()
                ],
                "agents": agents,
                "queue": queue,
            }

    def load_state(self, state):
        with self._lock:
            if not isinstance(state, dict):
                raise TypeError("state must be a dictionary")
            state = copy.deepcopy(state)
            state_version = state.get("version", self.version)
            state_revision = state.get("revision")

            state_polygons = None
            if state.get("polygons"):
                state_polygons = self._normalise_polygons(
                    OrderedDict(
                        (entry[0], entry[1]) for entry in state.get("polygons", [])
                    )
                )
            can_restore_mesh = (
                not self._polygons
                and state_polygons is not None
            )
            same_mesh = (
                bool(self._polygons)
                and state_polygons is not None
                and state_version == self.version
                and state_polygons == self._polygons
            )
            if can_restore_mesh:
                self._polygons = state_polygons
                walkable = state.get("walkable")
                if walkable:
                    self._walkable = OrderedDict(
                        (entry[0], bool(entry[1])) for entry in walkable
                    )
                else:
                    self._walkable = OrderedDict(
                        (pid, True) for pid in self._polygons
                    )
                self._links = self._normalise_links(
                    [
                        {
                            "from_polygon": entry[1],
                            "to_polygon": entry[2],
                            "one_way": entry[3],
                        }
                        for entry in state.get("links", [])
                    ]
                )
                self.version = state_version
                if state_revision is not None:
                    self._revision = state_revision
                else:
                    self._revision = self._mesh_fingerprint()
                self._pending_version = None
                self._pending_polygons = None
                self._pending_links = None
            elif same_mesh:
                walkable = state.get("walkable")
                if walkable:
                    self._walkable = OrderedDict(
                        (entry[0], bool(entry[1]))
                        for entry in walkable
                        if entry[0] in self._polygons
                    )
                self._links = self._normalise_links(
                    [
                        {
                            "from_polygon": entry[1],
                            "to_polygon": entry[2],
                            "one_way": entry[3],
                        }
                        for entry in state.get("links", [])
                    ]
                )
                if state_revision is not None:
                    self._revision = state_revision
                else:
                    self._revision = self._mesh_fingerprint()

            paths_resumable = (
                state_version == self.version
                and (
                    state_revision is None
                    or state_revision == self._revision
                )
            )

            self._agents = {}
            for agent in state.get("agents", []):
                agent_id = agent.get("agent_id")
                position = self._point_from_list(agent.get("position"))
                goal = self._point_from_list(agent.get("goal"))
                path = self._path_from_state(agent.get("path"))
                index = int(agent.get("index", 0))

                if path is not None:
                    if paths_resumable:
                        try:
                            self._validate_path(path, position or goal)
                        except NavMeshQueryError:
                            path = None
                            index = 0
                    else:
                        path = None
                        index = 0

                self._agents[agent_id] = {
                    "position": position,
                    "goal": goal,
                    "path": path,
                    "index": index,
                }

            self._queue = OrderedDict()
            for request in state.get("queue", []):
                agent_id = request.get("agent_id")
                goal = self._point_from_list(request.get("goal"))
                if goal is None:
                    continue
                restored = {"goal": goal}
                position = self._point_from_list(request.get("position"))
                if position is not None:
                    restored["position"] = position
                self._queue[agent_id] = restored

    # ------------------------------------------------------------------
    # Path search helpers
    # ------------------------------------------------------------------
    def _search(self, start_polygon, goal_polygon):
        if start_polygon == goal_polygon:
            return [start_polygon], []

        graph = self._build_graph()
        distances = {start_polygon: 0.0}
        previous = {}
        heap = [(0.0, 0, start_polygon)]
        counter = 1

        while heap:
            distance, _, polygon_id = heapq.heappop(heap)
            if distance > distances.get(polygon_id, math.inf) + _EPS:
                continue
            if polygon_id == goal_polygon:
                break
            for next_polygon, traversal in graph.get(polygon_id, []):
                candidate = distance + traversal["cost"]
                if candidate + _EPS < distances.get(next_polygon, math.inf):
                    distances[next_polygon] = candidate
                    previous[next_polygon] = (polygon_id, traversal)
                    heapq.heappush(heap, (candidate, counter, next_polygon))
                    counter += 1

        if goal_polygon not in distances:
            return None, None

        polygon_ids = [goal_polygon]
        traversals = []
        current = goal_polygon
        while current != start_polygon:
            parent, traversal = previous[current]
            traversals.append(traversal)
            polygon_ids.append(parent)
            current = parent
        polygon_ids.reverse()
        traversals.reverse()
        return polygon_ids, traversals

    def _build_graph(self):
        graph = {pid: [] for pid in self._polygons if self._walkable.get(pid)}
        best_edges = {}

        polygon_ids = list(graph)
        for left_index, left_id in enumerate(polygon_ids):
            left_polygon = self._polygons[left_id]
            for right_id in polygon_ids[left_index + 1 :]:
                portal = self._shared_portal(left_polygon, self._polygons[right_id])
                if portal is None:
                    continue
                cost = self._distance(self._polygon_centre(left_polygon), portal)
                cost += self._distance(
                    portal, self._polygon_centre(self._polygons[right_id])
                )
                edge = {"kind": "portal", "portal": portal, "cost": max(cost, _EPS)}
                self._store_edge(best_edges, left_id, right_id, edge)
                self._store_edge(best_edges, right_id, left_id, copy.deepcopy(edge))

        for link in self._links.values():
            from_polygon = link["from_polygon"]
            to_polygon = link["to_polygon"]
            if (
                from_polygon not in graph
                or to_polygon not in graph
                or from_polygon == to_polygon
            ):
                continue
            from_point = self._polygon_centre(self._polygons[from_polygon])
            to_point = self._polygon_centre(self._polygons[to_polygon])
            cost = self._distance(from_point, to_point)
            forward = {
                "kind": "link",
                "from_point": from_point,
                "to_point": to_point,
                "cost": max(cost, _EPS),
            }
            self._store_edge(best_edges, from_polygon, to_polygon, forward)
            if not link["one_way"]:
                reverse = {
                    "kind": "link",
                    "from_point": to_point,
                    "to_point": from_point,
                    "cost": max(cost, _EPS),
                }
                self._store_edge(best_edges, to_polygon, from_polygon, reverse)

        for (from_polygon, to_polygon), edge in best_edges.items():
            graph[from_polygon].append((to_polygon, copy.deepcopy(edge)))
        return graph

    @staticmethod
    def _store_edge(best_edges, from_polygon, to_polygon, edge):
        key = (from_polygon, to_polygon)
        current = best_edges.get(key)
        if current is None or edge["cost"] < current["cost"]:
            best_edges[key] = edge

    # ------------------------------------------------------------------
    # Geometry and validation helpers
    # ------------------------------------------------------------------
    def _normalise_polygons(self, polygons):
        normalised = OrderedDict()
        for polygon_id, vertices in polygons.items():
            normalised[polygon_id] = tuple(self._as_point(point) for point in vertices)
        return normalised

    def _normalise_links(self, links):
        normalised = OrderedDict()
        if links is None:
            return normalised
        if isinstance(links, dict):
            items = links.items()
        else:
            items = []
            for index, link in enumerate(links):
                if isinstance(link, dict):
                    link_id = link.get(
                        "link_id", link.get("id", link.get("name", index))
                    )
                    items.append((link_id, link))
                else:
                    items.append((index, link))

        for link_id, link in items:
            if isinstance(link, dict):
                from_polygon = link.get(
                    "from_polygon", link.get("from", link.get("source"))
                )
                to_polygon = link.get(
                    "to_polygon", link.get("to", link.get("target"))
                )
                one_way = link.get("one_way", link.get("directed", True))
            else:
                values = list(link)
                if len(values) == 4:
                    link_id, from_polygon, to_polygon, one_way = values
                else:
                    from_polygon, to_polygon = values[0], values[1]
                    one_way = values[2] if len(values) > 2 else True
            normalised[link_id] = {
                "from_polygon": from_polygon,
                "to_polygon": to_polygon,
                "one_way": bool(one_way),
            }
        return normalised

    def _walkable_polygon_at(self, point):
        point = self._as_point(point)
        for polygon_id, vertices in self._polygons.items():
            if not self._walkable.get(polygon_id, False):
                continue
            if self._point_in_polygon(point, vertices):
                return polygon_id
        return None

    def _require_walkable_polygon(self, point, message):
        polygon_id = self._walkable_polygon_at(point)
        if polygon_id is None:
            self._query_failure(point, message)
        return polygon_id

    def _query_failure(self, point, message):
        cell = self._cell_for(point)
        logger.error("%s at point %s (cell %s)", message, point, cell)
        raise NavMeshQueryError(
            "%s at point %s (cell %s)" % (message, point, cell), cell=cell
        )

    def _cell_for(self, point):
        point = self._as_point(point)
        size = self.cell_size if self.cell_size > 0 else 1.0
        return (
            int(math.floor(point[0] / size)),
            int(math.floor(point[1] / size)),
        )

    def _validate_path(self, path, position=None):
        if not isinstance(path, dict):
            self._query_failure(position or (0, 0), "path is not a path object")
        if path.get("version") != self.version:
            self._query_failure(
                position or path.get("goal") or (0, 0),
                "path belongs to stale navmesh version",
            )
        if "_revision" in path and path["_revision"] != self._revision:
            self._query_failure(
                position or path.get("goal") or (0, 0),
                "path belongs to stale navmesh revision",
            )
        points = path.get("points")
        if not points:
            self._query_failure(
                position or path.get("goal") or (0, 0), "path has no points"
            )
        for point in points:
            point = self._as_point(point)
            if self._walkable_polygon_at(point) is None:
                self._query_failure(point, "path point is outside walkable navmesh")

    def _snap_to_mesh(self, point):
        point = self._as_point(point)
        if self._walkable_polygon_at(point) is not None:
            return point
        if not self._polygons or not any(self._walkable.values()):
            self._query_failure(point, "cannot snap position: no walkable navmesh")
        return self._nearest_walkable_point(point)

    def _nearest_walkable_point(self, point):
        point = self._as_point(point)
        best = None
        best_distance = math.inf
        for polygon_id, vertices in self._polygons.items():
            if not self._walkable.get(polygon_id, False):
                continue
            candidate = self._nearest_point_on_polygon(point, vertices)
            distance = self._distance(point, candidate)
            if distance < best_distance:
                best = candidate
                best_distance = distance
        if best is None:
            self._query_failure(point, "cannot snap position: no walkable navmesh")
        return self._snap_exact(best)

    def _snap_exact(self, point):
        if self._walkable_polygon_at(point) is not None:
            return point
        for polygon_id, vertices in self._polygons.items():
            if not self._walkable.get(polygon_id, False):
                continue
            if self._point_in_polygon(point, vertices):
                return point
        return self._nearest_walkable_point(point)

    def _nearest_point_on_polygon(self, point, vertices):
        point = self._as_point(point)
        best = vertices[0]
        best_distance = math.inf
        count = len(vertices)
        for index in range(count):
            left = vertices[index]
            right = vertices[(index + 1) % count]
            candidate = self._closest_point_on_segment(point, left, right)
            distance = self._distance(point, candidate)
            if distance < best_distance:
                best = candidate
                best_distance = distance
        return best

    @staticmethod
    def _closest_point_on_segment(point, left, right):
        dx = right[0] - left[0]
        dy = right[1] - left[1]
        length_sq = dx * dx + dy * dy
        if length_sq <= _EPS:
            return left
        t = (
            (point[0] - left[0]) * dx + (point[1] - left[1]) * dy
        ) / length_sq
        t = max(0.0, min(1.0, t))
        return (left[0] + t * dx, left[1] + t * dy)

    @staticmethod
    def _point_in_polygon(point, vertices):
        if len(vertices) < 3:
            return False
        inside = False
        count = len(vertices)
        for index in range(count):
            left = vertices[index]
            right = vertices[(index + 1) % count]
            if CorridorFollow._point_on_segment(point, left, right):
                return True
            if (left[1] > point[1]) != (right[1] > point[1]):
                x_at_y = (right[0] - left[0]) * (
                    point[1] - left[1]
                ) / (right[1] - left[1]) + left[0]
                if point[0] < x_at_y:
                    inside = not inside
        return inside

    @staticmethod
    def _point_on_segment(point, left, right):
        cross = (right[0] - left[0]) * (point[1] - left[1]) - (
            right[1] - left[1]
        ) * (point[0] - left[0])
        scale = max(
            1.0,
            abs(right[0] - left[0]) + abs(right[1] - left[1]),
            abs(point[0] - left[0]) + abs(point[1] - left[1]),
        )
        if abs(cross) > _EPS * scale:
            return False
        dot = (point[0] - left[0]) * (right[0] - left[0]) + (
            point[1] - left[1]
        ) * (right[1] - left[1])
        if dot < -_EPS:
            return False
        length_sq = (right[0] - left[0]) ** 2 + (right[1] - left[1]) ** 2
        return dot <= length_sq + _EPS

    def _shared_portal(self, left_vertices, right_vertices):
        best = None
        best_length = 0.0
        shared_vertices = []
        for left in left_vertices:
            for right in right_vertices:
                if self._distance(left, right) <= 1e-7:
                    shared_vertices.append(left)
        if len(shared_vertices) >= 2:
            left = shared_vertices[0]
            right = max(shared_vertices, key=lambda item: self._distance(left, item))
            return ((left[0] + right[0]) * 0.5, (left[1] + right[1]) * 0.5)

        for left_index in range(len(left_vertices)):
            left_a = left_vertices[left_index]
            left_b = left_vertices[(left_index + 1) % len(left_vertices)]
            dx = left_b[0] - left_a[0]
            dy = left_b[1] - left_a[1]
            left_length_sq = dx * dx + dy * dy
            if left_length_sq <= _EPS:
                continue
            for right_index in range(len(right_vertices)):
                right_a = right_vertices[right_index]
                right_b = right_vertices[(right_index + 1) % len(right_vertices)]
                cross_a = dx * (right_a[1] - left_a[1]) - dy * (
                    right_a[0] - left_a[0]
                )
                cross_b = dx * (right_b[1] - left_a[1]) - dy * (
                    right_b[0] - left_a[0]
                )
                scale = max(1.0, math.sqrt(left_length_sq))
                if abs(cross_a) > 1e-7 * scale or abs(cross_b) > 1e-7 * scale:
                    continue
                right_a_t = (
                    (right_a[0] - left_a[0]) * dx
                    + (right_a[1] - left_a[1]) * dy
                ) / left_length_sq
                right_b_t = (
                    (right_b[0] - left_a[0]) * dx
                    + (right_b[1] - left_a[1]) * dy
                ) / left_length_sq
                start = max(0.0, min(right_a_t, right_b_t))
                end = min(1.0, max(right_a_t, right_b_t))
                if end - start <= 1e-7:
                    continue
                length = (end - start) * math.sqrt(left_length_sq)
                if length > best_length:
                    middle = (start + end) * 0.5
                    best = (
                        left_a[0] + middle * dx,
                        left_a[1] + middle * dy,
                    )
                    best_length = length

        if best is not None:
            return best
        return None

    @staticmethod
    def _polygon_centre(vertices):
        count = float(len(vertices))
        return (
            sum(point[0] for point in vertices) / count,
            sum(point[1] for point in vertices) / count,
        )

    def _distance_to_polyline(self, point, points, start_index):
        if len(points) < 2:
            return self._distance(point, points[0])
        start = max(0, min(start_index, len(points) - 2))
        best = math.inf
        for index in range(start, len(points) - 1):
            candidate = self._closest_point_on_segment(
                point, points[index], points[index + 1]
            )
            best = min(best, self._distance(point, candidate))
        return best

    def _advance_index(self, points, position, start_index):
        index = max(0, min(int(start_index), len(points) - 1))
        if index >= len(points) - 1:
            return len(points) - 1

        first_segment = max(0, index - 1)
        best_segment = first_segment
        best_distance = math.inf
        for segment in range(first_segment, len(points) - 1):
            left = points[segment]
            right = points[segment + 1]
            candidate = self._closest_point_on_segment(position, left, right)
            distance = self._distance(position, candidate)
            if distance + _EPS < best_distance:
                best_distance = distance
                best_segment = segment

        return max(index, min(best_segment + 1, len(points) - 1))

    @staticmethod
    def _distance(left, right):
        return math.hypot(left[0] - right[0], left[1] - right[1])

    @staticmethod
    def _append_point(points, point, force=False):
        point = (float(point[0]), float(point[1]))
        if force:
            points.append(point)
        elif not points or CorridorFollow._distance(points[-1], point) > 1e-9:
            points.append(point)

    def _paths_equivalent(self, left, right):
        if left.get("version") != right.get("version"):
            return False
        if (
            left.get("_revision") is not None
            and right.get("_revision") is not None
            and left.get("_revision") != right.get("_revision")
        ):
            return False
        if tuple(left.get("goal", ())) != tuple(right.get("goal", ())):
            return False
        left_points = tuple(tuple(point) for point in left.get("points", ()))
        right_points = tuple(tuple(point) for point in right.get("points", ()))
        return left_points == right_points

    @staticmethod
    def _as_point(point):
        if point is None or len(point) < 2:
            raise ValueError("point must contain x and y")
        return (float(point[0]), float(point[1]))

    def _normalise_path(self, path):
        if not isinstance(path, dict):
            self._query_failure((0, 0), "path is not a path object")
        path = copy.deepcopy(path)
        path["points"] = [self._as_point(point) for point in path.get("points", [])]
        if path.get("goal") is not None:
            path["goal"] = self._as_point(path["goal"])
        if path.get("start") is not None:
            path["start"] = self._as_point(path["start"])
        if path.get("polygons") is not None:
            path["polygons"] = list(path["polygons"])
        return path

    def _point_to_list(self, point):
        if point is None:
            return None
        return [float(point[0]), float(point[1])]

    def _point_from_list(self, point):
        if point is None:
            return None
        return self._as_point(point)

    def _path_to_state(self, path):
        if path is None:
            return None
        state = copy.deepcopy(path)
        state["points"] = [self._point_to_list(point) for point in path["points"]]
        state["goal"] = self._point_to_list(path.get("goal"))
        state["start"] = self._point_to_list(path.get("start"))
        return state

    def _path_from_state(self, path):
        if path is None:
            return None
        state = copy.deepcopy(path)
        state["points"] = [
            self._as_point(point) for point in state.get("points", [])
        ]
        if state.get("goal") is not None:
            state["goal"] = self._as_point(state["goal"])
        if state.get("start") is not None:
            state["start"] = self._as_point(state["start"])
        return state

    def _mesh_changed(self, bump_version):
        if bump_version:
            self._bump_version()
        self._revision = self._mesh_fingerprint()

    def _bump_version(self):
        if isinstance(self.version, int) and not isinstance(self.version, bool):
            self.version += 1

    def _mesh_fingerprint(self):
        payload = (
            self.version,
            tuple(
                (polygon_id, tuple(vertices))
                for polygon_id, vertices in self._polygons.items()
            ),
            tuple(self._walkable.items()),
            tuple(
                (
                    link_id,
                    link["from_polygon"],
                    link["to_polygon"],
                    link["one_way"],
                )
                for link_id, link in self._links.items()
            ),
        )
        return hashlib.sha256(repr(payload).encode("utf-8")).hexdigest()
