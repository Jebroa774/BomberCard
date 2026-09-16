"""Route selected DRC-reported ordinary-signal opens on In2.Cu.

The dedicated In1.Cu ground plane remains untouched.  Bottom-side endpoints
use adjacent B.Cu->In2.Cu microvias.  Top-side endpoints fan out to an ordinary
through via, which also reaches In2.Cu.  The protected +5V_RAW and +5V_AUX
polygons are treated as hard routing keepouts and all other PWR-layer zones are
refilled around accepted signal copper before saving the candidate.
"""

from __future__ import annotations

import argparse
import heapq
import json
import math
import re
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

import pcbnew


TRACK_WIDTH = 0.15
CLEARANCE = 0.25
GRID_STEP = 0.20


def mm(value: int) -> float:
    return pcbnew.ToMM(value)


def iu(value: float) -> int:
    return pcbnew.FromMM(value)


def point(value: tuple[float, float]) -> pcbnew.VECTOR2I:
    return pcbnew.VECTOR2I_MM(*value)


def parse_xy(value: str) -> tuple[float, float]:
    try:
        x, y = value.split(",", 1)
        return float(x), float(y)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("expected X,Y in millimetres") from exc


def distance_to_segment(
    p: tuple[float, float],
    a: tuple[float, float],
    b: tuple[float, float],
) -> float:
    vx, vy = b[0] - a[0], b[1] - a[1]
    wx, wy = p[0] - a[0], p[1] - a[1]
    length_sq = vx * vx + vy * vy
    if length_sq == 0:
        return math.hypot(wx, wy)
    t = max(0.0, min(1.0, (wx * vx + wy * vy) / length_sq))
    return math.hypot(p[0] - (a[0] + t * vx), p[1] - (a[1] + t * vy))


def segments_intersect(
    a: tuple[float, float],
    b: tuple[float, float],
    c: tuple[float, float],
    d: tuple[float, float],
) -> bool:
    def orientation(p, q, r):
        return (q[0] - p[0]) * (r[1] - p[1]) - (q[1] - p[1]) * (r[0] - p[0])

    o1, o2 = orientation(a, b, c), orientation(a, b, d)
    o3, o4 = orientation(c, d, a), orientation(c, d, b)
    return (o1 == 0 or o2 == 0 or o1 * o2 < 0) and (
        o3 == 0 or o4 == 0 or o3 * o4 < 0
    )


def segment_distance(
    a: tuple[float, float],
    b: tuple[float, float],
    c: tuple[float, float],
    d: tuple[float, float],
) -> float:
    if a == b:
        return distance_to_segment(a, c, d)
    if c == d:
        return distance_to_segment(c, a, b)
    if segments_intersect(a, b, c, d):
        return 0.0
    return min(
        distance_to_segment(a, c, d),
        distance_to_segment(b, c, d),
        distance_to_segment(c, a, b),
        distance_to_segment(d, a, b),
    )


@dataclass(frozen=True)
class Endpoint:
    net: str
    pos: tuple[float, float]
    layer: str
    description: str


@dataclass(frozen=True)
class Edge:
    net: str
    start: Endpoint
    end: Endpoint

    @property
    def length(self) -> float:
        return math.dist(self.start.pos, self.end.pos)


def parse_layer(description: str) -> str:
    if description.startswith("Durchsteckpad") or description.startswith("Via"):
        return "all"
    match = re.search(r" auf (F\.Cu|B\.Cu|PWR|GND|In1\.Cu|In2\.Cu)", description)
    if not match:
        raise RuntimeError(f"Cannot determine endpoint layer: {description}")
    return match.group(1)


def load_edges(report: Path) -> list[Edge]:
    data = json.loads(report.read_text(encoding="utf-8-sig"))
    result: list[Edge] = []
    for violation in data.get("unconnected_items", []):
        items = violation["items"]
        match = re.search(r"\[(/[^\]]+)\]", items[0]["description"])
        if not match:
            raise RuntimeError(f"Cannot determine net: {items[0]['description']}")
        net = match.group(1)
        endpoints = []
        for item in items:
            endpoints.append(
                Endpoint(
                    net=net,
                    pos=(float(item["pos"]["x"]), float(item["pos"]["y"])),
                    layer=parse_layer(item["description"]),
                    description=item["description"],
                )
            )
        result.append(Edge(net, endpoints[0], endpoints[1]))
    return result


class GridRouter:
    def __init__(
        self,
        board: pcbnew.BOARD,
        route_layer: int,
        step: float = GRID_STEP,
        block_in2_power_zones: bool = True,
    ) -> None:
        self.board = board
        self.route_layer = route_layer
        self.step = step
        self.block_in2_power_zones = block_in2_power_zones
        bbox = board.GetBoardEdgesBoundingBox()
        self.xmin = mm(bbox.GetX())
        self.ymin = mm(bbox.GetY())
        self.xmax = self.xmin + mm(bbox.GetWidth())
        self.ymax = self.ymin + mm(bbox.GetHeight())
        self.nx = int(math.ceil((self.xmax - self.xmin) / step)) + 1
        self.ny = int(math.ceil((self.ymax - self.ymin) / step)) + 1
        self.owners: dict[tuple[int, int], set[str]] = defaultdict(set)
        self.outline = pcbnew.SHAPE_POLY_SET()
        if not board.GetBoardPolygonOutlines(self.outline, False):
            raise RuntimeError("Could not build board outline")
        self._mark_boundaries_and_keepouts()
        self._mark_existing_route_copper()

    def index(self, p: tuple[float, float]) -> tuple[int, int]:
        return (
            int(round((p[0] - self.xmin) / self.step)),
            int(round((p[1] - self.ymin) / self.step)),
        )

    def coord(self, node: tuple[int, int]) -> tuple[float, float]:
        return self.xmin + node[0] * self.step, self.ymin + node[1] * self.step

    def _nodes_in_box(
        self, xmin: float, ymin: float, xmax: float, ymax: float
    ) -> list[tuple[int, int]]:
        ix0, iy0 = self.index((xmin, ymin))
        ix1, iy1 = self.index((xmax, ymax))
        return [
            (ix, iy)
            for ix in range(max(0, ix0 - 1), min(self.nx, ix1 + 2))
            for iy in range(max(0, iy0 - 1), min(self.ny, iy1 + 2))
        ]

    def _mark_boundaries_and_keepouts(self) -> None:
        edge_margin = 0.50
        for ix in range(self.nx):
            for iy in range(self.ny):
                p = self.coord((ix, iy))
                if (
                    p[0] < self.xmin + edge_margin
                    or p[0] > self.xmax - edge_margin
                    or p[1] < self.ymin + edge_margin
                    or p[1] > self.ymax - edge_margin
                    or not self.outline.PointInside(point(p))
                ):
                    self.owners[(ix, iy)].add("__BLOCK__")
        # PointInside rejects copper inside cut-outs, but it does not enforce
        # the configured copper-to-edge clearance around their boundary.  The
        # PocketLab card has a large rectangular lower-edge notch, so mark all
        # Edge.Cuts chords explicitly as clearance obstacles as well.
        edge_radius = edge_margin + TRACK_WIDTH / 2 + 0.03
        for drawing in self.board.GetDrawings():
            if drawing.GetLayer() != pcbnew.Edge_Cuts or not hasattr(drawing, "GetStart"):
                continue
            start = (mm(drawing.GetStart().x), mm(drawing.GetStart().y))
            end = (mm(drawing.GetEnd().x), mm(drawing.GetEnd().y))
            self.mark_segment(start, end, edge_radius, "__BLOCK__")
        rule_zones = [(zone, False) for zone in self.board.Zones()]
        for footprint in self.board.GetFootprints():
            rule_zones.extend((zone, True) for zone in footprint.Zones())
        for zone, footprint_embedded in rule_zones:
            if (
                zone.GetIsRuleArea()
                and (footprint_embedded or zone.IsOnLayer(self.route_layer))
                and (zone.GetDoNotAllowTracks() or zone.GetDoNotAllowVias())
            ):
                box = zone.GetBoundingBox()
                inflate = TRACK_WIDTH / 2 + CLEARANCE + 0.05
                self.mark_box(
                    mm(box.GetX()) - inflate,
                    mm(box.GetY()) - inflate,
                    mm(box.GetRight()) + inflate,
                    mm(box.GetBottom()) + inflate,
                    "__BLOCK__",
                )

        for zone in (
            self.board.Zones()
            if self.route_layer == pcbnew.In2_Cu and self.block_in2_power_zones
            else ()
        ):
            if (
                zone.GetNetname() not in {"/+5V_RAW", "/+5V_AUX"}
                or not zone.HasFilledPolysForLayer(pcbnew.In2_Cu)
            ):
                continue
            polygons = zone.GetFilledPolysList(pcbnew.In2_Cu)
            box = polygons.BBox()
            for node in self._nodes_in_box(
                mm(box.GetX()),
                mm(box.GetY()),
                mm(box.GetRight()),
                mm(box.GetBottom()),
            ):
                if polygons.PointInside(point(self.coord(node))):
                    self.owners[node].add("__BLOCK__")

    def mark_circle(
        self, center: tuple[float, float], radius: float, owner: str
    ) -> None:
        for node in self._nodes_in_box(
            center[0] - radius,
            center[1] - radius,
            center[0] + radius,
            center[1] + radius,
        ):
            if math.dist(self.coord(node), center) <= radius:
                self.owners[node].add(owner)

    def mark_box(
        self, xmin: float, ymin: float, xmax: float, ymax: float, owner: str
    ) -> None:
        for node in self._nodes_in_box(xmin, ymin, xmax, ymax):
            x, y = self.coord(node)
            if xmin <= x <= xmax and ymin <= y <= ymax:
                self.owners[node].add(owner)

    def mark_segment(
        self,
        start: tuple[float, float],
        end: tuple[float, float],
        radius: float,
        owner: str,
    ) -> None:
        for node in self._nodes_in_box(
            min(start[0], end[0]) - radius,
            min(start[1], end[1]) - radius,
            max(start[0], end[0]) + radius,
            max(start[1], end[1]) + radius,
        ):
            if distance_to_segment(self.coord(node), start, end) <= radius:
                self.owners[node].add(owner)

    def _mark_existing_route_copper(self) -> None:
        track_radius = TRACK_WIDTH / 2 + CLEARANCE
        for footprint in self.board.GetFootprints():
            for pad in footprint.Pads():
                if not pad.IsOnLayer(self.route_layer):
                    continue
                box = pad.GetBoundingBox()
                inflate = TRACK_WIDTH / 2 + CLEARANCE
                self.mark_box(
                    mm(box.GetX()) - inflate,
                    mm(box.GetY()) - inflate,
                    mm(box.GetRight()) + inflate,
                    mm(box.GetBottom()) + inflate,
                    pad.GetNetname() or "__BLOCK__",
                )
        for item in self.board.GetTracks():
            owner = item.GetNetname() or "__BLOCK__"
            if isinstance(item, pcbnew.PCB_VIA):
                if item.IsOnLayer(self.route_layer):
                    center = (mm(item.GetPosition().x), mm(item.GetPosition().y))
                    radius = mm(item.GetWidth(self.route_layer)) / 2 + track_radius
                    self.mark_circle(center, radius, owner)
            elif item.GetLayer() == self.route_layer:
                start = (mm(item.GetStart().x), mm(item.GetStart().y))
                end = (mm(item.GetEnd().x), mm(item.GetEnd().y))
                radius = mm(item.GetWidth()) / 2 + track_radius
                self.mark_segment(start, end, radius, owner)

    def is_free(self, node: tuple[int, int], net: str) -> bool:
        if not (0 <= node[0] < self.nx and 0 <= node[1] < self.ny):
            return False
        return not (self.owners.get(node, set()) - {net})

    def circle_is_free(
        self, center: tuple[float, float], radius: float, net: str
    ) -> bool:
        for node in self._nodes_in_box(
            center[0] - radius,
            center[1] - radius,
            center[0] + radius,
            center[1] + radius,
        ):
            if math.dist(self.coord(node), center) <= radius and not self.is_free(
                node, net
            ):
                return False
        return True

    def nearest_free_node(
        self, position: tuple[float, float], net: str
    ) -> tuple[int, int]:
        center = self.index(position)
        candidates = []
        for radius in range(12):
            for dx in range(-radius, radius + 1):
                for dy in range(-radius, radius + 1):
                    node = center[0] + dx, center[1] + dy
                    if self.is_free(node, net):
                        candidates.append((math.dist(position, self.coord(node)), node))
            if candidates:
                return min(candidates)[1]
        raise RuntimeError(f"No free grid access for {net}: {position}")

    def route(
        self, start: tuple[float, float], end: tuple[float, float], net: str
    ) -> list[tuple[float, float]]:
        source = self.nearest_free_node(start, net)
        target = self.nearest_free_node(end, net)
        directions = (
            (1, 0),
            (-1, 0),
            (0, 1),
            (0, -1),
            (1, 1),
            (1, -1),
            (-1, 1),
            (-1, -1),
        )
        queue: list[tuple[float, float, tuple[int, int], tuple[int, int]]] = []
        initial_dir = (0, 0)
        heapq.heappush(queue, (0.0, 0.0, source, initial_dir))
        best = {(source, initial_dir): 0.0}
        parent: dict[
            tuple[tuple[int, int], tuple[int, int]],
            tuple[tuple[int, int], tuple[int, int]],
        ] = {}
        found: tuple[tuple[int, int], tuple[int, int]] | None = None
        while queue:
            _, cost, node, previous_dir = heapq.heappop(queue)
            state = (node, previous_dir)
            if cost != best.get(state):
                continue
            if node == target:
                found = state
                break
            for direction in directions:
                nxt = node[0] + direction[0], node[1] + direction[1]
                if not self.is_free(nxt, net):
                    continue
                move = math.sqrt(2.0) if direction[0] and direction[1] else 1.0
                turn = 0.0 if previous_dir in ((0, 0), direction) else 0.18
                new_cost = cost + move + turn
                new_state = (nxt, direction)
                if new_cost >= best.get(new_state, math.inf):
                    continue
                best[new_state] = new_cost
                parent[new_state] = state
                dx, dy = target[0] - nxt[0], target[1] - nxt[1]
                heuristic = max(abs(dx), abs(dy)) + (math.sqrt(2) - 1) * min(
                    abs(dx), abs(dy)
                )
                heapq.heappush(
                    queue, (new_cost + heuristic, new_cost, nxt, direction)
                )
        if found is None:
            layer_name = self.board.GetLayerName(self.route_layer)
            raise RuntimeError(
                f"No {layer_name} grid path for {net}: {start} -> {end}"
            )
        nodes = []
        state = found
        while True:
            nodes.append(state[0])
            if state[0] == source and state[1] == initial_dir:
                break
            state = parent[state]
        nodes.reverse()
        compressed = [nodes[0]]
        previous = None
        for a, b in zip(nodes, nodes[1:]):
            direction = (b[0] - a[0], b[1] - a[1])
            if previous is not None and direction != previous:
                compressed.append(a)
            previous = direction
        compressed.append(nodes[-1])
        points = [start]
        points.extend(self.coord(node) for node in compressed[1:-1])
        points.append(end)
        for a, b in zip(points, points[1:]):
            self.mark_segment(
                a,
                b,
                TRACK_WIDTH / 2 + CLEARANCE + TRACK_WIDTH / 2 + 0.05,
                net,
            )
        return points


def add_track(
    board: pcbnew.BOARD,
    net: str,
    start: tuple[float, float],
    end: tuple[float, float],
    layer: int,
    width: float = TRACK_WIDTH,
) -> None:
    if start == end:
        return
    track = pcbnew.PCB_TRACK(board)
    track.SetStart(point(start))
    track.SetEnd(point(end))
    track.SetWidth(iu(width))
    track.SetLayer(layer)
    track.SetNet(board.FindNet(net))
    track.SetLocked(True)
    board.Add(track)


def add_via(
    board: pcbnew.BOARD,
    net: str,
    pos: tuple[float, float],
    via_type: int,
    layers: tuple[int, int],
    diameter: float,
    drill: float,
) -> None:
    via = pcbnew.PCB_VIA(board)
    via.SetPosition(point(pos))
    via.SetViaType(via_type)
    via.SetWidth(iu(diameter))
    via.SetDrill(iu(drill))
    via.SetLayerPair(*layers)
    via.SetNet(board.FindNet(net))
    via.SetLocked(True)
    board.Add(via)


def segment_clear_on_layer(
    board: pcbnew.BOARD,
    layer: int,
    net: str,
    start: tuple[float, float],
    end: tuple[float, float],
    item_radius: float = TRACK_WIDTH / 2,
) -> bool:
    required = item_radius + CLEARANCE
    length = math.dist(start, end)
    samples = max(1, int(math.ceil(length / 0.10)))
    for footprint in board.GetFootprints():
        for pad in footprint.Pads():
            if not pad.IsOnLayer(layer) or pad.GetNetname() == net:
                continue
            box = pad.GetBoundingBox()
            xmin, ymin = mm(box.GetX()), mm(box.GetY())
            xmax, ymax = mm(box.GetRight()), mm(box.GetBottom())
            for index in range(samples + 1):
                fraction = index / samples
                x = start[0] + fraction * (end[0] - start[0])
                y = start[1] + fraction * (end[1] - start[1])
                dx = max(xmin - x, 0.0, x - xmax)
                dy = max(ymin - y, 0.0, y - ymax)
                if math.hypot(dx, dy) < required:
                    return False
    for item in board.GetTracks():
        if item.GetNetname() == net:
            continue
        if isinstance(item, pcbnew.PCB_VIA):
            if item.IsOnLayer(layer):
                center = (mm(item.GetPosition().x), mm(item.GetPosition().y))
                radius = mm(item.GetWidth(layer)) / 2 + required
                if distance_to_segment(center, start, end) < radius:
                    return False
        elif item.GetLayer() == layer:
            other_start = (mm(item.GetStart().x), mm(item.GetStart().y))
            other_end = (mm(item.GetEnd().x), mm(item.GetEnd().y))
            if segment_distance(start, end, other_start, other_end) < (
                mm(item.GetWidth()) / 2 + required
            ):
                return False
    return True


def same_net_track_sites(
    board: pcbnew.BOARD,
    endpoint: Endpoint,
    layer: int,
) -> tuple[tuple[float, float], ...]:
    """Return nearby points on the DRC endpoint's same-net copper branch.

    KiCad often reports the nearest end of a very short existing track as the
    second item of an open connection.  That exact point can be too cramped
    for a via even though another point on the same copper branch is legal.
    """
    if not endpoint.description.startswith(("Leiterbahn", "Track")):
        return ()
    candidates: list[tuple[float, tuple[float, float]]] = []
    for item in board.GetTracks():
        if isinstance(item, pcbnew.PCB_VIA):
            continue
        if item.GetLayer() != layer or item.GetNetname() != endpoint.net:
            continue
        start = (mm(item.GetStart().x), mm(item.GetStart().y))
        end = (mm(item.GetEnd().x), mm(item.GetEnd().y))
        for fraction in (0.0, 0.25, 0.5, 0.75, 1.0):
            site = (
                start[0] + fraction * (end[0] - start[0]),
                start[1] + fraction * (end[1] - start[1]),
            )
            candidates.append((math.dist(endpoint.pos, site), site))
    result: list[tuple[float, float]] = []
    seen: set[tuple[int, int]] = set()
    for _, site in sorted(candidates):
        key = (round(site[0] * 1000), round(site[1] * 1000))
        if key in seen:
            continue
        seen.add(key)
        result.append(site)
    return tuple(result)


def nearby_existing_via_site(
    board: pcbnew.BOARD,
    endpoint: Endpoint,
    route_layer: int,
) -> tuple[float, float] | None:
    if not endpoint.description.startswith(("Leiterbahn", "Track")):
        return None
    candidates: list[tuple[float, tuple[float, float]]] = []
    for item in board.GetTracks():
        if not isinstance(item, pcbnew.PCB_VIA):
            continue
        spans_route_layer = (
            item.GetViaType() == pcbnew.VIATYPE_THROUGH or item.IsOnLayer(route_layer)
        )
        if item.GetNetname() != endpoint.net or not spans_route_layer:
            continue
        site = (mm(item.GetPosition().x), mm(item.GetPosition().y))
        separation = math.dist(endpoint.pos, site)
        if separation <= 1.25:
            candidates.append((separation, site))
    return min(candidates)[1] if candidates else None


def transition_to_in2(
    board: pcbnew.BOARD,
    router: GridRouter,
    endpoint: Endpoint,
    cache: dict[tuple[str, float, float, str], tuple[float, float]],
) -> tuple[float, float]:
    key = (endpoint.net, endpoint.pos[0], endpoint.pos[1], endpoint.layer)
    if key in cache:
        return cache[key]
    p = endpoint.pos
    route_layer_name = board.GetLayerName(router.route_layer)
    if router.route_layer in (pcbnew.F_Cu, pcbnew.B_Cu):
        if endpoint.layer == "all" or endpoint.layer == route_layer_name:
            cache[key] = p
            return p
        opposite_layer_name = "F.Cu" if router.route_layer == pcbnew.B_Cu else "B.Cu"
        opposite_layer = pcbnew.F_Cu if endpoint.layer == "F.Cu" else pcbnew.B_Cu
        if endpoint.layer != opposite_layer_name:
            raise RuntimeError(
                f"Endpoint is not on {route_layer_name}: {endpoint.description}"
            )
        outer_offsets = (
            (-0.60, 0.00), (0.60, 0.00), (0.00, -0.60), (0.00, 0.60),
            (-0.45, -0.45), (-0.45, 0.45), (0.45, -0.45), (0.45, 0.45),
            (-0.85, 0.00), (0.85, 0.00), (0.00, -0.85), (0.00, 0.85),
            (-1.10, 0.00), (1.10, 0.00), (0.00, -1.10), (0.00, 1.10),
            (-1.40, 0.00), (1.40, 0.00), (0.00, -1.40), (0.00, 1.40),
            (-1.80, 0.00), (1.80, 0.00), (0.00, -1.80), (0.00, 1.80),
            (-2.20, 0.00), (2.20, 0.00), (0.00, -2.20), (0.00, 2.20),
        )
        via_radius = 0.225
        sites = tuple(
            ((p[0] + dx, p[1] + dy), bool(dx or dy))
            for dx, dy in ((0.0, 0.0),) + outer_offsets
        )
        sites += tuple(
            (site, False)
            for site in same_net_track_sites(board, endpoint, opposite_layer)
        )
        surface = None
        surface_needs_link = False
        for candidate, needs_link in sites:
            if needs_link and not segment_clear_on_layer(
                board, opposite_layer, endpoint.net, p, candidate
            ):
                continue
            if any(
                not segment_clear_on_layer(
                    board,
                    layer,
                    endpoint.net,
                    candidate,
                    candidate,
                    item_radius=via_radius,
                )
                for layer in (pcbnew.F_Cu, pcbnew.In1_Cu, pcbnew.In2_Cu, pcbnew.B_Cu)
            ):
                continue
            if not router.circle_is_free(candidate, via_radius, endpoint.net):
                continue
            surface = candidate
            surface_needs_link = needs_link
            break
        if surface is None:
            raise RuntimeError(
                f"No outer-layer through-via escape for {endpoint.description}"
            )
        if surface_needs_link:
            add_track(board, endpoint.net, p, surface, opposite_layer)
        add_via(
            board,
            endpoint.net,
            surface,
            pcbnew.VIATYPE_THROUGH,
            (pcbnew.F_Cu, pcbnew.B_Cu),
            0.45,
            0.20,
        )
        cache[key] = surface
        return surface
    offsets = (
        (-0.60, 0.00),
        (0.60, 0.00),
        (0.00, -0.60),
        (0.00, 0.60),
        (-0.45, -0.45),
        (-0.45, 0.45),
        (0.45, -0.45),
        (0.45, 0.45),
        (-0.85, 0.00),
        (0.85, 0.00),
        (0.00, -0.85),
        (0.00, 0.85),
        (-1.10, 0.00),
        (1.10, 0.00),
        (0.00, -1.10),
        (0.00, 1.10),
        (-0.80, -0.80),
        (-0.80, 0.80),
        (0.80, -0.80),
        (0.80, 0.80),
        (-1.40, 0.00),
        (1.40, 0.00),
        (0.00, -1.40),
        (0.00, 1.40),
        (-1.20, -1.20),
        (-1.20, 1.20),
        (1.20, -1.20),
        (1.20, 1.20),
        (-1.80, 0.00),
        (1.80, 0.00),
        (0.00, -1.80),
        (0.00, 1.80),
        (-2.20, 0.00),
        (2.20, 0.00),
        (0.00, -2.20),
        (0.00, 2.20),
    )
    transition_radius = None
    direct_inner_layers = {route_layer_name}
    if router.route_layer == pcbnew.In1_Cu:
        direct_inner_layers.update(("GND", "In1.Cu"))
    elif router.route_layer == pcbnew.In2_Cu:
        direct_inner_layers.update(("PWR", "In2.Cu"))
    if endpoint.layer == "all" or endpoint.layer in direct_inner_layers:
        q = p
    elif (existing_via := nearby_existing_via_site(board, endpoint, router.route_layer)) is not None:
        q = existing_via
    elif (
        endpoint.layer == "PWR" and router.route_layer == pcbnew.In1_Cu
    ) or (
        endpoint.layer == "GND" and router.route_layer == pcbnew.In2_Cu
    ):
        surface_layer = pcbnew.In2_Cu if endpoint.layer == "PWR" else pcbnew.In1_Cu
        via_radius = 0.15
        transition_radius = via_radius + TRACK_WIDTH / 2 + CLEARANCE + 0.05
        sites = (p,) + same_net_track_sites(board, endpoint, surface_layer)
        surface = None
        for candidate in sites:
            if any(
                not segment_clear_on_layer(
                    board,
                    layer,
                    endpoint.net,
                    candidate,
                    candidate,
                    item_radius=via_radius,
                )
                for layer in (pcbnew.In1_Cu, pcbnew.In2_Cu)
            ):
                continue
            if not router.circle_is_free(candidate, via_radius, endpoint.net):
                continue
            surface = candidate
            break
        if surface is None:
            raise RuntimeError(
                f"No inner-layer microvia escape for {endpoint.description}"
            )
        add_via(
            board,
            endpoint.net,
            surface,
            pcbnew.VIATYPE_MICROVIA,
            (pcbnew.In1_Cu, pcbnew.In2_Cu),
            0.30,
            0.10,
        )
        q = surface
    elif endpoint.layer == "F.Cu":
        use_microvia = router.route_layer == pcbnew.In1_Cu
        via_radius = 0.15 if use_microvia else 0.225
        transition_radius = via_radius + TRACK_WIDTH / 2 + CLEARANCE + 0.05
        surface = None
        sites = tuple(((p[0] + dx, p[1] + dy), bool(dx or dy)) for dx, dy in ((0.0, 0.0),) + offsets)
        sites += tuple((site, False) for site in same_net_track_sites(board, endpoint, pcbnew.F_Cu))
        surface_needs_link = False
        for candidate, needs_link in sites:
            if needs_link and not segment_clear_on_layer(
                board, pcbnew.F_Cu, endpoint.net, p, candidate
            ):
                continue
            via_layers = (
                (pcbnew.F_Cu, pcbnew.In1_Cu)
                if use_microvia
                else (pcbnew.F_Cu, pcbnew.In1_Cu, pcbnew.In2_Cu, pcbnew.B_Cu)
            )
            if any(
                not segment_clear_on_layer(
                    board,
                    layer,
                    endpoint.net,
                    candidate,
                    candidate,
                    item_radius=via_radius,
                )
                for layer in via_layers
            ):
                continue
            if not router.circle_is_free(candidate, via_radius, endpoint.net):
                continue
            surface = candidate
            surface_needs_link = needs_link
            break
        if surface is None:
            raise RuntimeError(f"No F.Cu through-via escape for {endpoint.description}")
        if surface_needs_link:
            add_track(board, endpoint.net, p, surface, pcbnew.F_Cu)
        if use_microvia:
            add_via(
                board,
                endpoint.net,
                surface,
                pcbnew.VIATYPE_MICROVIA,
                (pcbnew.F_Cu, pcbnew.In1_Cu),
                0.30,
                0.10,
            )
        else:
            add_via(
                board,
                endpoint.net,
                surface,
                pcbnew.VIATYPE_THROUGH,
                (pcbnew.F_Cu, pcbnew.B_Cu),
                0.45,
                0.20,
            )
        q = surface
    elif endpoint.layer == "B.Cu":
        use_staggered_microvias = router.route_layer == pcbnew.In1_Cu
        use_microvia = router.route_layer == pcbnew.In2_Cu
        via_radius = 0.15 if (use_microvia or use_staggered_microvias) else 0.225
        transition_radius = via_radius + TRACK_WIDTH / 2 + CLEARANCE + 0.05
        surface = None
        sites = tuple(((p[0] + dx, p[1] + dy), bool(dx or dy)) for dx, dy in ((0.0, 0.0),) + offsets)
        sites += tuple((site, False) for site in same_net_track_sites(board, endpoint, pcbnew.B_Cu))
        surface_needs_link = False
        for candidate, needs_link in sites:
            if needs_link and not segment_clear_on_layer(
                board, pcbnew.B_Cu, endpoint.net, p, candidate
            ):
                continue
            via_layers = (
                (pcbnew.B_Cu, pcbnew.In2_Cu)
                if use_staggered_microvias
                else (
                    (pcbnew.B_Cu, pcbnew.In2_Cu)
                    if use_microvia
                    else (pcbnew.F_Cu, pcbnew.In1_Cu, pcbnew.In2_Cu, pcbnew.B_Cu)
                )
            )
            if any(
                not segment_clear_on_layer(
                    board,
                    layer,
                    endpoint.net,
                    candidate,
                    candidate,
                    item_radius=via_radius,
                )
                for layer in via_layers
            ):
                continue
            if not router.circle_is_free(candidate, via_radius, endpoint.net):
                continue
            surface = candidate
            surface_needs_link = needs_link
            break
        if surface is None:
            via_kind = "microvia" if use_microvia else "through-via"
            raise RuntimeError(f"No B.Cu {via_kind} escape for {endpoint.description}")
        inner_site = None
        if use_staggered_microvias:
            inner_offsets = (
                (-0.40, 0.00), (0.40, 0.00), (0.00, -0.40), (0.00, 0.40),
                (-0.45, -0.45), (-0.45, 0.45), (0.45, -0.45), (0.45, 0.45),
                (-0.60, 0.00), (0.60, 0.00), (0.00, -0.60), (0.00, 0.60),
                (-0.80, 0.00), (0.80, 0.00), (0.00, -0.80), (0.00, 0.80),
            )
            for dx, dy in inner_offsets:
                candidate = surface[0] + dx, surface[1] + dy
                if not segment_clear_on_layer(
                    board, pcbnew.In2_Cu, endpoint.net, surface, candidate
                ):
                    continue
                if any(
                    not segment_clear_on_layer(
                        board,
                        layer,
                        endpoint.net,
                        candidate,
                        candidate,
                        item_radius=0.15,
                    )
                    for layer in (pcbnew.In2_Cu, pcbnew.In1_Cu)
                ):
                    continue
                if not router.circle_is_free(candidate, 0.15, endpoint.net):
                    continue
                inner_site = candidate
                break
            if inner_site is None:
                raise RuntimeError(
                    f"No staggered In2.Cu->In1.Cu microvia for {endpoint.description}"
                )
        q = inner_site if inner_site is not None else surface
        if surface_needs_link:
            add_track(board, endpoint.net, p, surface, pcbnew.B_Cu)
        if use_microvia:
            add_via(
                board,
                endpoint.net,
                surface,
                pcbnew.VIATYPE_MICROVIA,
                (pcbnew.In2_Cu, pcbnew.B_Cu),
                0.30,
                0.10,
            )
        elif use_staggered_microvias:
            add_via(
                board,
                endpoint.net,
                surface,
                pcbnew.VIATYPE_MICROVIA,
                (pcbnew.In2_Cu, pcbnew.B_Cu),
                0.30,
                0.10,
            )
            assert inner_site is not None
            add_track(
                board,
                endpoint.net,
                surface,
                inner_site,
                pcbnew.In2_Cu,
            )
            add_via(
                board,
                endpoint.net,
                inner_site,
                pcbnew.VIATYPE_MICROVIA,
                (pcbnew.In1_Cu, pcbnew.In2_Cu),
                0.30,
                0.10,
            )
        else:
            add_via(
                board,
                endpoint.net,
                surface,
                pcbnew.VIATYPE_THROUGH,
                (pcbnew.F_Cu, pcbnew.B_Cu),
                0.45,
                0.20,
            )
    else:
        raise RuntimeError(f"Unsupported endpoint layer: {endpoint.layer}")
    if transition_radius is not None:
        router.mark_circle(q, transition_radius, endpoint.net)
    cache[key] = q
    return q


def main() -> int:
    global CLEARANCE
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--drc", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--net", action="append")
    parser.add_argument(
        "--skip-net",
        action="append",
        default=[],
        help="Ignore this net while selecting DRC-reported open edges",
    )
    parser.add_argument(
        "--route-layer",
        choices=("F.Cu", "In1.Cu", "In2.Cu", "B.Cu"),
        default="In2.Cu",
    )
    parser.add_argument("--limit", type=int)
    parser.add_argument("--min-length", type=float)
    parser.add_argument("--max-length", type=float)
    parser.add_argument("--shortest-first", action="store_true")
    parser.add_argument("--clearance", type=float, default=CLEARANCE)
    parser.add_argument("--grid", type=float, default=GRID_STEP)
    parser.add_argument(
        "--ignore-in2-power-zones",
        action="store_true",
        help="Allow routing through refillable +5V In2 zones; refill and DRC must validate the result",
    )
    parser.add_argument(
        "--manual-start",
        type=parse_xy,
        help="Use this X,Y point directly on --route-layer instead of a DRC endpoint",
    )
    parser.add_argument(
        "--manual-end",
        type=parse_xy,
        help="Use this X,Y point directly on --route-layer instead of a DRC endpoint",
    )
    parser.add_argument(
        "--continue-on-failure",
        action="store_true",
        help="Route a batch transactionally, skipping edges that have no clean path",
    )
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    if args.input.resolve() == args.output.resolve():
        raise RuntimeError("Output must differ from input")
    if args.output.exists() and not args.force:
        raise RuntimeError(f"Output exists; use --force: {args.output}")

    CLEARANCE = args.clearance

    board = pcbnew.LoadBoard(str(args.input.resolve()))
    if args.manual_start is not None or args.manual_end is not None:
        if args.manual_start is None or args.manual_end is None:
            raise RuntimeError("--manual-start and --manual-end must be used together")
        if not args.net or len(args.net) != 1:
            raise RuntimeError("manual endpoints require exactly one --net")
        net = args.net[0]
        edges = [
            Edge(
                net,
                Endpoint(net, args.manual_start, args.route_layer, "manual start"),
                Endpoint(net, args.manual_end, args.route_layer, "manual end"),
            )
        ]
    else:
        edges = load_edges(args.drc)
        if args.net:
            selected = set(args.net)
            edges = [edge for edge in edges if edge.net in selected]
    if args.skip_net:
        skipped = set(args.skip_net)
        edges = [edge for edge in edges if edge.net not in skipped]
    if args.min_length is not None:
        edges = [edge for edge in edges if edge.length >= args.min_length]
    if args.max_length is not None:
        edges = [edge for edge in edges if edge.length <= args.max_length]
    edges.sort(key=lambda edge: edge.length, reverse=not args.shortest_first)
    if args.limit is not None:
        edges = edges[: args.limit]
    route_layers = {
        "F.Cu": pcbnew.F_Cu,
        "In1.Cu": pcbnew.In1_Cu,
        "In2.Cu": pcbnew.In2_Cu,
        "B.Cu": pcbnew.B_Cu,
    }
    route_layer = route_layers[args.route_layer]
    routed = 0
    if args.continue_on_failure:
        snapshot = args.output.with_name(args.output.stem + "-attempt.kicad_pcb")
        router = GridRouter(
            board,
            route_layer,
            step=args.grid,
            block_in2_power_zones=not args.ignore_in2_power_zones,
        )
        for index, edge in enumerate(edges, start=1):
            pcbnew.SaveBoard(str(snapshot.resolve()), board)
            try:
                cache: dict[tuple[str, float, float, str], tuple[float, float]] = {}
                start = transition_to_in2(board, router, edge.start, cache)
                end = transition_to_in2(board, router, edge.end, cache)
                path = router.route(start, end, edge.net)
                for a, b in zip(path, path[1:]):
                    add_track(board, edge.net, a, b, route_layer)
                    router.mark_segment(
                        a,
                        b,
                        TRACK_WIDTH / 2 + CLEARANCE,
                        edge.net,
                    )
                routed += 1
                print(
                    f"ROUTED {routed} ({index}/{len(edges)}) {edge.net} "
                    f"{edge.length:.3f} mm {len(path)-1} segments",
                    flush=True,
                )
            except RuntimeError as exc:
                board = pcbnew.LoadBoard(str(snapshot.resolve()))
                print(
                    f"SKIPPED {index}/{len(edges)} {edge.net} {edge.length:.3f} mm: {exc}",
                    flush=True,
                )
        snapshot.unlink(missing_ok=True)
    else:
        router = GridRouter(
            board,
            route_layer,
            step=args.grid,
            block_in2_power_zones=not args.ignore_in2_power_zones,
        )
        cache: dict[tuple[str, float, float, str], tuple[float, float]] = {}
        for edge in edges:
            transition_to_in2(board, router, edge.start, cache)
            transition_to_in2(board, router, edge.end, cache)
        for edge in edges:
            start = cache[(edge.net, edge.start.pos[0], edge.start.pos[1], edge.start.layer)]
            end = cache[(edge.net, edge.end.pos[0], edge.end.pos[1], edge.end.layer)]
            path = router.route(start, end, edge.net)
            for a, b in zip(path, path[1:]):
                add_track(board, edge.net, a, b, route_layer)
            routed += 1
            print(f"ROUTED {routed}/{len(edges)} {edge.net} {edge.length:.3f} mm {len(path)-1} segments")

    if not pcbnew.ZONE_FILLER(board).Fill(board.Zones()):
        raise RuntimeError("Zone refill failed")
    pcbnew.SaveBoard(str(args.output.resolve()), board)
    reloaded = pcbnew.LoadBoard(str(args.output.resolve()))
    reloaded.BuildConnectivity()
    connectivity = reloaded.GetConnectivity()
    connectivity.RecalculateRatsnest()
    print(f"Saved {args.output}: routed={routed}, opens={connectivity.GetUnconnectedCount(False)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
