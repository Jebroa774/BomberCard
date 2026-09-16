"""Add deterministic short GND/+3V3 fanouts to the two inner planes.

L2 is the uninterrupted GND plane and L3 is the +3V3 plane.  KiCad cannot
connect an outer-layer SMD pad directly to either inner zone, so every isolated
outer copper cluster needs at least one ordinary through via.  This helper
adds one short, locked pad-to-via fanout per cluster while retaining all
existing placement, tracks, vias, drawings and zones.

The placement is geometry-aware but intentionally conservative: vias are kept
outside SMD lands, board edges and existing different-net copper.  Any cluster
for which no reviewed-clear candidate can be found is reported and left in the
ratsnest for a manual pass.  KiCad DRC with zone refill remains the acceptance
test.
"""

from __future__ import annotations

import argparse
import heapq
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import pcbnew


F = pcbnew.F_Cu
B = pcbnew.B_Cu
TARGET_NETS = {
    "/GND": 0.20,
    "/+3V3": 0.20,
}
VIA_DIAMETER_MM = 0.50
VIA_DRILL_MM = 0.30
DIFFERENT_NET_CLEARANCE_MM = 0.25
SAME_NET_SPACING_MM = 0.08
EDGE_CLEARANCE_MM = 0.50
GRID_STEP_MM = 0.25
GRID_MAX_RADIUS_MM = 4.50
EXISTING_VIA_MAX_DISTANCE_MM = 6.0
FINE_PITCH_PLANE_ESCAPE_REFS = frozenset(
    {
        "J2", "J8", "U2", "U6", "U7", "U8", "U9", "U10", "U11",
        "U15", "U17", "U18", "U19", "U24", "U25",
    }
)
LF_FINE_PITCH_REFS = frozenset({"U1", "U4", "U17", "U18", "U21", "U22"})
PLANE_LAYER = {
    "/GND": pcbnew.In1_Cu,
    "/+3V3": pcbnew.In2_Cu,
}

# U2.3 sits on a 0.50-mm pitch next to NFC_TX1.  A normal 0.20-mm fanout
# cannot leave the pad while satisfying the project's 0.25-mm NFC clearance;
# keep this one connection for a reviewed neckdown/manual route.
MANUAL_FANOUT_PADS = frozenset({("U2", "3")})


@dataclass(frozen=True)
class Rect:
    left: float
    top: float
    right: float
    bottom: float

    def expanded(self, amount: float) -> "Rect":
        return Rect(
            self.left - amount,
            self.top - amount,
            self.right + amount,
            self.bottom + amount,
        )

    def contains(self, point: tuple[float, float]) -> bool:
        x, y = point
        return self.left <= x <= self.right and self.top <= y <= self.bottom

    def intersects(self, other: "Rect") -> bool:
        return not (
            self.right < other.left
            or other.right < self.left
            or self.bottom < other.top
            or other.bottom < self.top
        )


SUBGHZ_NOTCH = Rect(66.20, 67.40, 85.00, 74.00)


@dataclass
class CopperObstacle:
    net: str
    kind: str
    geometry: object
    owner: object | None = None


def mm(value: int) -> float:
    return pcbnew.ToMM(value)


def point(x_mm: float, y_mm: float) -> pcbnew.VECTOR2I:
    return pcbnew.VECTOR2I(pcbnew.FromMM(x_mm), pcbnew.FromMM(y_mm))


def xy(position: pcbnew.VECTOR2I) -> tuple[float, float]:
    return mm(position.x), mm(position.y)


def rect_of(item: pcbnew.BOARD_ITEM) -> Rect:
    box = item.GetBoundingBox()
    return Rect(mm(box.GetLeft()), mm(box.GetTop()), mm(box.GetRight()), mm(box.GetBottom()))


def distance(a: tuple[float, float], b: tuple[float, float]) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])


def point_segment_distance(
    p: tuple[float, float], a: tuple[float, float], b: tuple[float, float]
) -> float:
    dx = b[0] - a[0]
    dy = b[1] - a[1]
    if dx == 0.0 and dy == 0.0:
        return distance(p, a)
    t = ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / (dx * dx + dy * dy)
    t = max(0.0, min(1.0, t))
    nearest = (a[0] + t * dx, a[1] + t * dy)
    return distance(p, nearest)


def orientation(a: tuple[float, float], b: tuple[float, float], c: tuple[float, float]) -> float:
    return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])


def segments_intersect(
    a: tuple[float, float],
    b: tuple[float, float],
    c: tuple[float, float],
    d: tuple[float, float],
) -> bool:
    o1 = orientation(a, b, c)
    o2 = orientation(a, b, d)
    o3 = orientation(c, d, a)
    o4 = orientation(c, d, b)
    epsilon = 1e-9

    def on_segment(
        first: tuple[float, float],
        second: tuple[float, float],
        candidate: tuple[float, float],
    ) -> bool:
        return (
            min(first[0], second[0]) - epsilon
            <= candidate[0]
            <= max(first[0], second[0]) + epsilon
            and min(first[1], second[1]) - epsilon
            <= candidate[1]
            <= max(first[1], second[1]) + epsilon
        )

    if o1 * o2 < -epsilon and o3 * o4 < -epsilon:
        return True
    if abs(o1) <= epsilon and on_segment(a, b, c):
        return True
    if abs(o2) <= epsilon and on_segment(a, b, d):
        return True
    if abs(o3) <= epsilon and on_segment(c, d, a):
        return True
    if abs(o4) <= epsilon and on_segment(c, d, b):
        return True
    return False


def segment_distance(
    a: tuple[float, float],
    b: tuple[float, float],
    c: tuple[float, float],
    d: tuple[float, float],
) -> float:
    if segments_intersect(a, b, c, d):
        return 0.0
    return min(
        point_segment_distance(a, c, d),
        point_segment_distance(b, c, d),
        point_segment_distance(c, a, b),
        point_segment_distance(d, a, b),
    )


def segment_intersects_rect(
    a: tuple[float, float], b: tuple[float, float], rect: Rect
) -> bool:
    if rect.contains(a) or rect.contains(b):
        return True
    corners = (
        (rect.left, rect.top),
        (rect.right, rect.top),
        (rect.right, rect.bottom),
        (rect.left, rect.bottom),
    )
    return any(
        segments_intersect(a, b, corners[index], corners[(index + 1) % 4])
        for index in range(4)
    )


def pad_layer(pad: pcbnew.PAD) -> int | None:
    layers = set(pad.GetLayerSet().Seq())
    if F in layers and B not in layers:
        return F
    if B in layers and F not in layers:
        return B
    return None


def is_through_pad(pad: pcbnew.PAD) -> bool:
    layers = set(pad.GetLayerSet().Seq())
    return F in layers and B in layers


def item_key(item: pcbnew.BOARD_ITEM) -> str:
    return str(item.m_Uuid.AsString())


def plane_track_pad_clearance(net_name: str, pad: pcbnew.PAD) -> float:
    if (
        net_name in TARGET_NETS
        and pad.GetParentFootprint().GetReference() in FINE_PITCH_PLANE_ESCAPE_REFS
    ):
        return 0.20
    if net_name.startswith("/LF_"):
        return DIFFERENT_NET_CLEARANCE_MM
    if net_name in {"/SPI_SCK", "/SPI_MOSI", "/SPI_MISO"}:
        return DIFFERENT_NET_CLEARANCE_MM
    return DIFFERENT_NET_CLEARANCE_MM


def plane_via_pad_clearance(net_name: str, pad: pcbnew.PAD) -> float:
    if (
        net_name in TARGET_NETS
        and pad.GetParentFootprint().GetReference() in FINE_PITCH_PLANE_ESCAPE_REFS
    ):
        return 0.20
    return DIFFERENT_NET_CLEARANCE_MM


def isolated_clusters(board: pcbnew.BOARD, net_name: str) -> list[list[pcbnew.PAD]]:
    board.BuildConnectivity()
    connectivity = board.GetConnectivity()
    pads = [
        pad
        for footprint in board.GetFootprints()
        for pad in footprint.Pads()
        if pad.GetNetname() == net_name
        and pad_layer(pad) is not None
        and (footprint.GetReference(), str(pad.GetNumber())) not in MANUAL_FANOUT_PADS
    ]
    clusters: dict[tuple[str, ...], list[pcbnew.PAD]] = {}
    for pad in pads:
        connected = list(connectivity.GetConnectedItems(pad))
        key = tuple(sorted(item_key(item) for item in connected))
        clusters.setdefault(key, []).append(pad)

    result: list[list[pcbnew.PAD]] = []
    for group in clusters.values():
        connected = list(connectivity.GetConnectedItems(group[0]))
        if any(isinstance(item, pcbnew.PCB_VIA) for item in connected):
            continue
        if any(isinstance(item, pcbnew.PAD) and is_through_pad(item) for item in connected):
            continue
        result.append(group)
    return result


def board_rect(board: pcbnew.BOARD) -> Rect:
    box = board.GetBoardEdgesBoundingBox()
    return Rect(mm(box.GetLeft()), mm(box.GetTop()), mm(box.GetRight()), mm(box.GetBottom()))


def existing_obstacles(board: pcbnew.BOARD) -> list[CopperObstacle]:
    obstacles: list[CopperObstacle] = []
    for footprint in board.GetFootprints():
        for pad in footprint.Pads():
            obstacles.append(CopperObstacle(pad.GetNetname(), "pad", rect_of(pad), pad))
        # Custom antenna/loop footprints contain copper graphics which have no
        # assignable net in KiCad but remain real copper obstacles.
        for graphic in footprint.GraphicalItems():
            if graphic.GetLayer() in (F, B):
                obstacles.append(
                    CopperObstacle("", "copper_graphic", (rect_of(graphic), graphic.GetLayer()), graphic)
                )
        for zone in footprint.Zones():
            if not zone.GetIsRuleArea():
                continue
            obstacles.append(
                CopperObstacle(
                    "",
                    "keepout",
                    (
                        rect_of(zone),
                        set(zone.GetLayerSet().Seq()),
                        zone.GetDoNotAllowTracks(),
                        zone.GetDoNotAllowVias(),
                        zone.GetDoNotAllowZoneFills(),
                    ),
                    zone,
                )
            )
    for graphic in board.GetDrawings():
        if graphic.GetLayer() in (F, B):
            obstacles.append(
                CopperObstacle("", "copper_graphic", (rect_of(graphic), graphic.GetLayer()), graphic)
            )
    for zone in board.Zones():
        if not zone.GetIsRuleArea():
            continue
        obstacles.append(
            CopperObstacle(
                "",
                "keepout",
                (
                    rect_of(zone),
                    set(zone.GetLayerSet().Seq()),
                    zone.GetDoNotAllowTracks(),
                    zone.GetDoNotAllowVias(),
                    zone.GetDoNotAllowZoneFills(),
                ),
                zone,
            )
        )
    for item in board.GetTracks():
        if isinstance(item, pcbnew.PCB_VIA):
            obstacles.append(
                CopperObstacle(
                    item.GetNetname(),
                    "via",
                    (xy(item.GetPosition()), mm(item.GetWidth(F)) / 2.0),
                    item,
                )
            )
        else:
            obstacles.append(
                CopperObstacle(
                    item.GetNetname(),
                    "track",
                    (xy(item.GetStart()), xy(item.GetEnd()), mm(item.GetWidth()) / 2.0, item.GetLayer()),
                    item,
                )
            )
    return obstacles


def normalized(x_value: float, y_value: float) -> tuple[float, float]:
    length = math.hypot(x_value, y_value)
    if length < 1e-9:
        return 1.0, 0.0
    return x_value / length, y_value / length


def candidate_directions(pad: pcbnew.PAD) -> Iterable[tuple[float, float]]:
    pad_position = xy(pad.GetPosition())
    footprint_position = xy(pad.GetParentFootprint().GetPosition())
    outward = normalized(
        pad_position[0] - footprint_position[0], pad_position[1] - footprint_position[1]
    )
    angle = math.atan2(outward[1], outward[0])
    for delta_degrees in (0, 35, -35, 70, -70, 110, -110, 145, -145, 180):
        candidate_angle = angle + math.radians(delta_degrees)
        yield math.cos(candidate_angle), math.sin(candidate_angle)


def candidate_is_clear(
    *,
    net_name: str,
    layer: int,
    start: tuple[float, float],
    end: tuple[float, float],
    width_mm: float,
    source_pads: set[str],
    edge: Rect,
    obstacles: list[CopperObstacle],
) -> bool:
    via_radius = VIA_DIAMETER_MM / 2.0
    # Add a small numerical margin beyond the nominal edge constraint so
    # nanometre rounding cannot turn an exactly-on-limit fanout into a DRC hit.
    if not edge.expanded(-(EDGE_CLEARANCE_MM + via_radius + 0.05)).contains(end):
        return False
    notch_clearance = VIA_DIAMETER_MM / 2.0 + EDGE_CLEARANCE_MM
    if SUBGHZ_NOTCH.expanded(notch_clearance).contains(end):
        return False
    if segment_intersects_rect(
        start, end, SUBGHZ_NOTCH.expanded(width_mm / 2.0 + EDGE_CLEARANCE_MM)
    ):
        return False

    for obstacle in obstacles:
        same_net = obstacle.net == net_name
        clearance = SAME_NET_SPACING_MM if same_net else DIFFERENT_NET_CLEARANCE_MM
        if obstacle.kind == "pad":
            pad = obstacle.owner
            assert isinstance(pad, pcbnew.PAD)
            pad_id = item_key(pad)
            pad_rect = obstacle.geometry
            assert isinstance(pad_rect, Rect)
            if pad_id in source_pads:
                if pad_rect.expanded(via_radius + SAME_NET_SPACING_MM).contains(end):
                    return False
                continue
            via_clearance = SAME_NET_SPACING_MM if same_net else plane_via_pad_clearance(net_name, pad)
            if pad_rect.expanded(via_radius + via_clearance).contains(end):
                return False
            pad_layers = set(pad.GetLayerSet().Seq())
            track_clearance = (
                SAME_NET_SPACING_MM
                if same_net
                else plane_track_pad_clearance(net_name, pad)
            )
            if layer in pad_layers and segment_intersects_rect(
                start, end, pad_rect.expanded(width_mm / 2.0 + track_clearance)
            ):
                return False
        elif obstacle.kind == "via":
            center, radius = obstacle.geometry
            if distance(end, center) < via_radius + radius + clearance:
                return False
            if point_segment_distance(center, start, end) < radius + width_mm / 2.0 + clearance:
                return False
        elif obstacle.kind == "track":
            other_start, other_end, other_radius, other_layer = obstacle.geometry
            if point_segment_distance(end, other_start, other_end) < via_radius + other_radius + clearance:
                return False
            if other_layer == layer and segment_distance(start, end, other_start, other_end) < (
                width_mm / 2.0 + other_radius + clearance
            ):
                return False
        elif obstacle.kind == "copper_graphic":
            graphic_rect, graphic_layer = obstacle.geometry
            if graphic_rect.expanded(via_radius + DIFFERENT_NET_CLEARANCE_MM).contains(end):
                return False
            if graphic_layer == layer and segment_intersects_rect(
                start,
                end,
                graphic_rect.expanded(width_mm / 2.0 + DIFFERENT_NET_CLEARANCE_MM),
            ):
                return False
        elif obstacle.kind == "keepout":
            keepout_rect, layers, disallow_tracks, disallow_vias, disallow_zone_fills = (
                obstacle.geometry
            )
            if disallow_vias and keepout_rect.expanded(via_radius).contains(end):
                return False
            if (
                disallow_zone_fills
                and PLANE_LAYER[net_name] in layers
                and keepout_rect.expanded(via_radius).contains(end)
            ):
                return False
            if disallow_tracks and layer in layers and segment_intersects_rect(
                start, end, keepout_rect.expanded(width_mm / 2.0)
            ):
                return False
    return True


def track_segment_is_clear(
    *,
    net_name: str,
    layer: int,
    start: tuple[float, float],
    end: tuple[float, float],
    width_mm: float,
    source_pads: set[str],
    edge: Rect,
    obstacles: list[CopperObstacle],
) -> bool:
    if net_name in {"/SPI_SCK", "/SPI_MOSI", "/SPI_MISO"}:
        route_clearance = DIFFERENT_NET_CLEARANCE_MM
    else:
        route_clearance = DIFFERENT_NET_CLEARANCE_MM
    margin = EDGE_CLEARANCE_MM + width_mm / 2.0 + 0.05
    allowed_edge = edge.expanded(-margin)
    if not allowed_edge.contains(start) or not allowed_edge.contains(end):
        return False
    if segment_intersects_rect(
        start, end, SUBGHZ_NOTCH.expanded(width_mm / 2.0 + EDGE_CLEARANCE_MM)
    ):
        return False

    for obstacle in obstacles:
        if obstacle.net == net_name:
            # Merging into existing copper of the same net is desirable here;
            # it lets several nearby pads share one plane via.
            continue
        if obstacle.kind == "pad":
            pad = obstacle.owner
            assert isinstance(pad, pcbnew.PAD)
            if item_key(pad) in source_pads:
                continue
            drill = pad.GetDrillSize()
            has_hole = max(mm(drill.x), mm(drill.y)) > 0.0
            if layer not in set(pad.GetLayerSet().Seq()) and not has_hole:
                continue
            pad_rect = obstacle.geometry
            assert isinstance(pad_rect, Rect)
            if segment_intersects_rect(
                start,
                end,
                pad_rect.expanded(
                    width_mm / 2.0
                    + max(
                        plane_track_pad_clearance(net_name, pad),
                        0.25 if has_hole else 0.0,
                    )
                ),
            ):
                return False
        elif obstacle.kind == "via":
            via = obstacle.owner
            if isinstance(via, pcbnew.PCB_VIA):
                copper_order = (pcbnew.F_Cu, pcbnew.In1_Cu, pcbnew.In2_Cu, pcbnew.B_Cu)
                try:
                    first = copper_order.index(via.TopLayer())
                    last = copper_order.index(via.BottomLayer())
                except ValueError:
                    pass
                else:
                    lo, hi = sorted((first, last))
                    if layer not in copper_order[lo : hi + 1]:
                        # Blind/microvias do not obstruct tracks on copper
                        # layers that they do not touch.  Treating every via
                        # as through-board made legal inner-layer corridors
                        # appear completely sealed.
                        continue
            center, radius = obstacle.geometry
            if point_segment_distance(center, start, end) < (
                radius + width_mm / 2.0 + route_clearance
            ):
                return False
        elif obstacle.kind == "track":
            other_start, other_end, other_radius, other_layer = obstacle.geometry
            if other_layer == layer and segment_distance(start, end, other_start, other_end) < (
                width_mm / 2.0 + other_radius + route_clearance
            ):
                return False
        elif obstacle.kind == "copper_graphic":
            graphic_rect, graphic_layer = obstacle.geometry
            if graphic_layer == layer and segment_intersects_rect(
                start,
                end,
                graphic_rect.expanded(width_mm / 2.0 + route_clearance),
            ):
                return False
        elif obstacle.kind == "keepout":
            keepout_rect, layers, disallow_tracks, _, _ = obstacle.geometry
            if disallow_tracks and layer in layers and segment_intersects_rect(
                start, end, keepout_rect.expanded(width_mm / 2.0)
            ):
                return False
    return True


def via_point_is_clear(
    *,
    net_name: str,
    end: tuple[float, float],
    source_pads: set[str],
    edge: Rect,
    obstacles: list[CopperObstacle],
) -> bool:
    via_radius = VIA_DIAMETER_MM / 2.0
    if not edge.expanded(-(EDGE_CLEARANCE_MM + via_radius + 0.05)).contains(end):
        return False
    if SUBGHZ_NOTCH.expanded(via_radius + EDGE_CLEARANCE_MM).contains(end):
        return False
    for obstacle in obstacles:
        same_net = obstacle.net == net_name
        clearance = SAME_NET_SPACING_MM if same_net else DIFFERENT_NET_CLEARANCE_MM
        if obstacle.kind == "pad":
            pad = obstacle.owner
            assert isinstance(pad, pcbnew.PAD)
            pad_rect = obstacle.geometry
            assert isinstance(pad_rect, Rect)
            if item_key(pad) in source_pads:
                if pad_rect.expanded(via_radius + SAME_NET_SPACING_MM).contains(end):
                    return False
            elif pad_rect.expanded(
                via_radius + plane_via_pad_clearance(net_name, pad)
            ).contains(end):
                return False
        elif obstacle.kind == "via":
            center, radius = obstacle.geometry
            if distance(end, center) < via_radius + radius + clearance:
                return False
        elif obstacle.kind == "track":
            other_start, other_end, other_radius, _ = obstacle.geometry
            if not same_net and point_segment_distance(end, other_start, other_end) < (
                via_radius + other_radius + clearance
            ):
                return False
        elif obstacle.kind == "copper_graphic":
            graphic_rect, _ = obstacle.geometry
            if graphic_rect.expanded(via_radius + DIFFERENT_NET_CLEARANCE_MM).contains(end):
                return False
        elif obstacle.kind == "keepout":
            keepout_rect, layers, _, disallow_vias, disallow_zone_fills = obstacle.geometry
            if disallow_vias and keepout_rect.expanded(via_radius).contains(end):
                return False
            if (
                disallow_zone_fills
                and PLANE_LAYER[net_name] in layers
                and keepout_rect.expanded(via_radius).contains(end)
            ):
                return False
    return True


def nearby_obstacles(
    obstacles: list[CopperObstacle], center: tuple[float, float], radius: float
) -> list[CopperObstacle]:
    search = Rect(
        center[0] - radius,
        center[1] - radius,
        center[0] + radius,
        center[1] + radius,
    )
    result: list[CopperObstacle] = []
    for obstacle in obstacles:
        if obstacle.kind == "pad":
            obstacle_rect = obstacle.geometry
        elif obstacle.kind == "via":
            via_center, via_radius = obstacle.geometry
            obstacle_rect = Rect(
                via_center[0] - via_radius,
                via_center[1] - via_radius,
                via_center[0] + via_radius,
                via_center[1] + via_radius,
            )
        elif obstacle.kind == "track":
            track_start, track_end, track_radius, _ = obstacle.geometry
            obstacle_rect = Rect(
                min(track_start[0], track_end[0]) - track_radius,
                min(track_start[1], track_end[1]) - track_radius,
                max(track_start[0], track_end[0]) + track_radius,
                max(track_start[1], track_end[1]) + track_radius,
            )
        elif obstacle.kind in {"copper_graphic", "keepout"}:
            obstacle_rect = obstacle.geometry[0]
        else:
            continue
        assert isinstance(obstacle_rect, Rect)
        if search.intersects(obstacle_rect):
            result.append(obstacle)
    return result


def simplify_grid_path(
    points: list[tuple[float, float]],
) -> tuple[tuple[float, float], ...]:
    if len(points) <= 2:
        return tuple(points)
    simplified = [points[0]]
    previous_direction: tuple[int, int] | None = None
    for index in range(1, len(points)):
        dx = round(points[index][0] - points[index - 1][0], 6)
        dy = round(points[index][1] - points[index - 1][1], 6)
        direction = (0 if dx == 0 else (1 if dx > 0 else -1), 0 if dy == 0 else (1 if dy > 0 else -1))
        if previous_direction is not None and direction != previous_direction:
            simplified.append(points[index - 1])
        previous_direction = direction
    simplified.append(points[-1])
    return compact_path(simplified)


def find_grid_fanout(
    pad: pcbnew.PAD,
    cluster: list[pcbnew.PAD],
    obstacles: list[CopperObstacle],
    edge: Rect,
) -> tuple[tuple[float, float], ...] | None:
    net_name = pad.GetNetname()
    layer = pad_layer(pad)
    if layer is None:
        return None
    start = xy(pad.GetPosition())
    source_pad_ids = {item_key(item) for item in cluster}
    step = GRID_STEP_MM
    maximum_radius = GRID_MAX_RADIUS_MM
    local_obstacles = nearby_obstacles(obstacles, start, maximum_radius + 1.0)
    queue: list[tuple[float, int, int]] = [(0.0, 0, 0)]
    cost: dict[tuple[int, int], float] = {(0, 0): 0.0}
    previous: dict[tuple[int, int], tuple[int, int]] = {}
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
    while queue:
        current_cost, ix, iy = heapq.heappop(queue)
        key = (ix, iy)
        if current_cost > cost.get(key, math.inf) + 1e-9:
            continue
        current = (start[0] + ix * step, start[1] + iy * step)
        if current_cost >= 0.65 and via_point_is_clear(
            net_name=net_name,
            end=current,
            source_pads=source_pad_ids,
            edge=edge,
            obstacles=local_obstacles,
        ):
            keys = [key]
            while keys[-1] != (0, 0):
                keys.append(previous[keys[-1]])
            keys.reverse()
            path = [(start[0] + x_index * step, start[1] + y_index * step) for x_index, y_index in keys]
            return simplify_grid_path(path)
        for dx_index, dy_index in directions:
            next_key = (ix + dx_index, iy + dy_index)
            next_point = (
                start[0] + next_key[0] * step,
                start[1] + next_key[1] * step,
            )
            if distance(start, next_point) > maximum_radius:
                continue
            if not track_segment_is_clear(
                net_name=net_name,
                layer=layer,
                start=current,
                end=next_point,
                width_mm=TARGET_NETS[net_name],
                source_pads=source_pad_ids,
                edge=edge,
                obstacles=local_obstacles,
            ):
                continue
            step_cost = step * (math.sqrt(2.0) if dx_index and dy_index else 1.0)
            candidate_cost = current_cost + step_cost
            if candidate_cost + 1e-9 >= cost.get(next_key, math.inf):
                continue
            cost[next_key] = candidate_cost
            previous[next_key] = key
            heapq.heappush(queue, (candidate_cost, *next_key))
    return None


def add_grid_fanout(
    board: pcbnew.BOARD,
    cluster: list[pcbnew.PAD],
    obstacles: list[CopperObstacle],
    edge: Rect,
) -> tuple[list[pcbnew.PCB_TRACK], pcbnew.PCB_VIA] | None:
    for pad in sorted(cluster, key=lambda item: item.GetNumber()):
        route_points = find_grid_fanout(pad, cluster, obstacles, edge)
        if route_points is None:
            continue
        net_name = pad.GetNetname()
        net = board.FindNet(net_name)
        layer = pad_layer(pad)
        if net is None or layer is None:
            raise RuntimeError(f"Required plane net/layer is missing: {net_name}")
        tracks: list[pcbnew.PCB_TRACK] = []
        for index in range(len(route_points) - 1):
            segment = pcbnew.PCB_TRACK(board)
            segment.SetStart(point(*route_points[index]))
            segment.SetEnd(point(*route_points[index + 1]))
            segment.SetWidth(pcbnew.FromMM(TARGET_NETS[net_name]))
            segment.SetLayer(layer)
            segment.SetNet(net)
            segment.SetLocked(True)
            board.Add(segment)
            obstacles.append(
                CopperObstacle(
                    net_name,
                    "track",
                    (
                        route_points[index],
                        route_points[index + 1],
                        TARGET_NETS[net_name] / 2.0,
                        layer,
                    ),
                    segment,
                )
            )
            tracks.append(segment)
        via_position = route_points[-1]
        via = pcbnew.PCB_VIA(board)
        via.SetPosition(point(*via_position))
        via.SetWidth(pcbnew.FromMM(VIA_DIAMETER_MM))
        via.SetDrill(pcbnew.FromMM(VIA_DRILL_MM))
        via.SetViaType(pcbnew.VIATYPE_THROUGH)
        via.SetLayerPair(F, B)
        via.SetNet(net)
        via.SetLocked(True)
        board.Add(via)
        obstacles.append(
            CopperObstacle(net_name, "via", (via_position, VIA_DIAMETER_MM / 2.0), via)
        )
        return tracks, via
    return None


def compact_path(points: Iterable[tuple[float, float]]) -> tuple[tuple[float, float], ...]:
    result: list[tuple[float, float]] = []
    for candidate in points:
        if not result or distance(candidate, result[-1]) > 0.001:
            result.append(candidate)
    return tuple(result)


def path_candidates(
    start: tuple[float, float], end: tuple[float, float]
) -> Iterable[tuple[tuple[float, float], ...]]:
    yield compact_path((start, end))
    dx = end[0] - start[0]
    dy = end[1] - start[1]
    x_sign = 1.0 if dx >= 0.0 else -1.0
    y_sign = 1.0 if dy >= 0.0 else -1.0
    if abs(dx) >= abs(dy):
        yield compact_path((start, (end[0] - x_sign * abs(dy), start[1]), end))
        yield compact_path((start, (start[0] + x_sign * abs(dy), end[1]), end))
    else:
        yield compact_path((start, (start[0], end[1] - y_sign * abs(dx)), end))
        yield compact_path((start, (end[0], start[1] + y_sign * abs(dx)), end))
    # Orthogonal doglegs are a last local escape option.  The routes remain
    # short and can be visually reviewed; RF/switching nets are not handled by
    # this plane-fanout helper.
    yield compact_path((start, (end[0], start[1]), end))
    yield compact_path((start, (start[0], end[1]), end))


def connect_to_existing_via(
    board: pcbnew.BOARD,
    cluster: list[pcbnew.PAD],
    obstacles: list[CopperObstacle],
    edge: Rect,
) -> list[pcbnew.PCB_TRACK] | None:
    net_name = cluster[0].GetNetname()
    width_mm = TARGET_NETS[net_name]
    source_pad_ids = {item_key(pad) for pad in cluster}
    via_targets = [
        (obstacle.geometry[0], obstacle)
        for obstacle in obstacles
        if obstacle.kind == "via" and obstacle.net == net_name
    ]
    if not via_targets:
        return None
    for pad in sorted(cluster, key=lambda item: item.GetNumber()):
        layer = pad_layer(pad)
        if layer is None:
            continue
        start = xy(pad.GetPosition())
        nearby = sorted(via_targets, key=lambda item: distance(start, item[0]))
        for target, _ in nearby[:16]:
            if distance(start, target) > EXISTING_VIA_MAX_DISTANCE_MM:
                break
            for route_points in path_candidates(start, target):
                if any(
                    not track_segment_is_clear(
                        net_name=net_name,
                        layer=layer,
                        start=route_points[index],
                        end=route_points[index + 1],
                        width_mm=width_mm,
                        source_pads=source_pad_ids,
                        edge=edge,
                        obstacles=obstacles,
                    )
                    for index in range(len(route_points) - 1)
                ):
                    continue
                net = board.FindNet(net_name)
                if net is None:
                    raise RuntimeError(f"Required plane net is missing: {net_name}")
                tracks: list[pcbnew.PCB_TRACK] = []
                for index in range(len(route_points) - 1):
                    segment = pcbnew.PCB_TRACK(board)
                    segment.SetStart(point(*route_points[index]))
                    segment.SetEnd(point(*route_points[index + 1]))
                    segment.SetWidth(pcbnew.FromMM(width_mm))
                    segment.SetLayer(layer)
                    segment.SetNet(net)
                    segment.SetLocked(True)
                    board.Add(segment)
                    obstacles.append(
                        CopperObstacle(
                            net_name,
                            "track",
                            (
                                route_points[index],
                                route_points[index + 1],
                                width_mm / 2.0,
                                layer,
                            ),
                            segment,
                        )
                    )
                    tracks.append(segment)
                return tracks
    return None


def add_fanout(
    board: pcbnew.BOARD,
    cluster: list[pcbnew.PAD],
    obstacles: list[CopperObstacle],
    edge: Rect,
) -> tuple[pcbnew.PCB_TRACK, pcbnew.PCB_VIA] | None:
    net_name = cluster[0].GetNetname()
    width_mm = TARGET_NETS[net_name]
    source_pad_ids = {item_key(pad) for pad in cluster}
    # Try smaller pads first. Their available escape directions are generally
    # more constrained than large capacitor or connector lands.
    ordered_pads = sorted(
        cluster,
        key=lambda pad: rect_of(pad).right - rect_of(pad).left + rect_of(pad).bottom - rect_of(pad).top,
    )
    for pad in ordered_pads:
        layer = pad_layer(pad)
        if layer is None:
            continue
        start = xy(pad.GetPosition())
        pad_box = rect_of(pad)
        half_extent = max(
            start[0] - pad_box.left,
            pad_box.right - start[0],
            start[1] - pad_box.top,
            pad_box.bottom - start[1],
        )
        base_distance = max(0.65, half_extent + VIA_DIAMETER_MM / 2.0 + 0.12)
        for direction in candidate_directions(pad):
            for extra_distance in (0.0, 0.25, 0.50, 0.80, 1.10):
                route_distance = base_distance + extra_distance
                end = (
                    start[0] + direction[0] * route_distance,
                    start[1] + direction[1] * route_distance,
                )
                if not candidate_is_clear(
                    net_name=net_name,
                    layer=layer,
                    start=start,
                    end=end,
                    width_mm=width_mm,
                    source_pads=source_pad_ids,
                    edge=edge,
                    obstacles=obstacles,
                ):
                    continue

                net = board.FindNet(net_name)
                if net is None:
                    raise RuntimeError(f"Required plane net is missing: {net_name}")
                track = pcbnew.PCB_TRACK(board)
                track.SetStart(point(*start))
                track.SetEnd(point(*end))
                track.SetWidth(pcbnew.FromMM(width_mm))
                track.SetLayer(layer)
                track.SetNet(net)
                track.SetLocked(True)
                board.Add(track)

                via = pcbnew.PCB_VIA(board)
                via.SetPosition(point(*end))
                via.SetWidth(pcbnew.FromMM(VIA_DIAMETER_MM))
                via.SetDrill(pcbnew.FromMM(VIA_DRILL_MM))
                via.SetViaType(pcbnew.VIATYPE_THROUGH)
                via.SetLayerPair(F, B)
                via.SetNet(net)
                via.SetLocked(True)
                board.Add(via)

                obstacles.append(
                    CopperObstacle(net_name, "track", (start, end, width_mm / 2.0, layer), track)
                )
                obstacles.append(
                    CopperObstacle(net_name, "via", (end, VIA_DIAMETER_MM / 2.0), via)
                )
                return track, via
    return None


def route(board: pcbnew.BOARD) -> tuple[int, int, int, list[str]]:
    edge = board_rect(board)
    obstacles = existing_obstacles(board)
    added = 0
    skipped_groups: list[list[pcbnew.PAD]] = []
    for net_name in TARGET_NETS:
        clusters = isolated_clusters(board, net_name)
        for cluster in clusters:
            result = add_fanout(board, cluster, obstacles, edge)
            if result is None:
                skipped_groups.append(cluster)
            else:
                added += 1
    shared = 0
    grid_candidates: list[list[pcbnew.PAD]] = []
    for cluster in skipped_groups:
        if connect_to_existing_via(board, cluster, obstacles, edge) is not None:
            shared += 1
            continue
        grid_candidates.append(cluster)
    grid = 0
    final_skipped: list[str] = []
    for cluster in grid_candidates:
        if add_grid_fanout(board, cluster, obstacles, edge) is not None:
            grid += 1
            continue
        labels = sorted(
            f"{pad.GetParentFootprint().GetReference()}.{pad.GetNumber()}" for pad in cluster
        )
        final_skipped.append(f"{cluster[0].GetNetname()}:{'/'.join(labels)}")
    return added, shared, grid, final_skipped


def validate(board: pcbnew.BOARD) -> None:
    if board.FindNet("/GND") is None or board.FindNet("/+3V3") is None:
        raise RuntimeError("GND/+3V3 plane nets are missing")
    ground_zones = [zone for zone in board.Zones() if zone.GetNetname() == "/GND"]
    power_zones = [zone for zone in board.Zones() if zone.GetNetname() == "/+3V3"]
    if not ground_zones or not power_zones:
        raise RuntimeError("Expected L2 GND and L3 +3V3 zones are missing")
    # Migrated boards legitimately contain older locked plane vias with other
    # reviewed dimensions.  Do not mistake those for vias generated by this
    # pass; the post-save KiCad DRC remains the geometry acceptance check.


def main() -> int:
    hardware_dir = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    input_path = args.input.resolve()
    output_path = args.output.resolve()
    protected_main = (hardware_dir / "PocketLab-Card.kicad_pcb").resolve()
    if not input_path.is_file():
        raise RuntimeError(f"Input PCB does not exist: {input_path}")
    if output_path == protected_main:
        raise RuntimeError("Refusing to overwrite the main PCB directly")
    if output_path.exists() and not args.force:
        raise RuntimeError(f"Output exists; use --force to replace it: {output_path}")

    board = pcbnew.LoadBoard(str(input_path))
    added, shared, grid, skipped = route(board)
    validate(board)
    pcbnew.ZONE_FILLER(board).Fill(board.Zones())
    pcbnew.SaveBoard(str(output_path), board)
    reloaded = pcbnew.LoadBoard(str(output_path))
    validate(reloaded)
    print(
        f"Saved plane-fanout PCB: {output_path}; via_fanouts={added}; "
        f"shared_via_fanouts={shared}; grid_fanouts={grid}; skipped={len(skipped)}"
    )
    for entry in skipped:
        print(f"SKIPPED {entry}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
