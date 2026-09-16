"""Bridge power-plane islands cut apart by a reviewed inner-layer signal route.

The long-signal router may carve a continuous channel through an In2 power
zone.  This pass detects filled polygons on opposite sides of the selected
signal segments and reconnects the affected power islands with two adjacent-
layer vias plus one short bridge track.  An internal bridge is supported for
dense areas where neither outer layer has a legal local crossing.
"""

from __future__ import annotations

import argparse
import math
from dataclasses import dataclass
from pathlib import Path
import shutil

import pcbnew

import route_plane_fanouts as fanout
import route_lf_global as maze
from route_plane_fanouts import CopperObstacle, board_rect, existing_obstacles, point, xy
from route_remaining_signals import disconnected_pad_groups


@dataclass(frozen=True)
class Bridge:
    net_name: str
    left_group: int
    right_group: int
    path: tuple[tuple[float, float], ...]
    layer: int
    via_start: bool = True
    via_end: bool = True

    @property
    def start(self) -> tuple[float, float]:
        return self.path[0]

    @property
    def end(self) -> tuple[float, float]:
        return self.path[-1]

    @property
    def length(self) -> float:
        return sum(math.dist(first, second) for first, second in zip(self.path, self.path[1:]))


def filled_zone(
    board: pcbnew.BOARD, net_name: str, plane_layer: int
) -> pcbnew.ZONE:
    zones = [
        zone
        for zone in board.Zones()
        if zone.GetNetname() == net_name
        and zone.HasFilledPolysForLayer(plane_layer)
    ]
    if len(zones) != 1:
        raise RuntimeError(
            f"Expected one filled In2 zone for {net_name}, got {len(zones)}"
        )
    return zones[0]


def outline_group_map(
    board: pcbnew.BOARD,
    net_name: str,
    polygons: pcbnew.SHAPE_POLY_SET,
) -> dict[int, int]:
    """Associate each filled outline with its electrical pad group."""

    result: dict[int, int] = {}
    groups = disconnected_pad_groups(board, net_name)
    connectivity = board.GetConnectivity()
    for group_index, group in enumerate(groups):
        for item in connectivity.GetConnectedItems(group[0]):
            if not isinstance(item, pcbnew.PCB_TRACK) or item.Type() != pcbnew.PCB_VIA_T:
                continue
            position = item.GetPosition()
            for outline_index in range(polygons.OutlineCount()):
                if polygons.Outline(outline_index).PointInside(position, 1, True):
                    previous = result.setdefault(outline_index, group_index)
                    if previous != group_index:
                        raise RuntimeError(
                            f"Filled outline {outline_index} belongs to multiple {net_name} groups"
                        )
    return result


def group_at(
    position: tuple[float, float],
    polygons: pcbnew.SHAPE_POLY_SET,
    outline_groups: dict[int, int],
) -> int | None:
    location = point(*position)
    matches = {
        group_index
        for outline_index, group_index in outline_groups.items()
        if polygons.Outline(outline_index).BBox().Contains(location)
        and polygons.Outline(outline_index).PointInside(location, 1, True)
    }
    return next(iter(matches)) if len(matches) == 1 else None


def cutter_segments(
    board: pcbnew.BOARD, cutter_net: str, plane_layer: int
) -> list[tuple[tuple[float, float], tuple[float, float]]]:
    return [
        (xy(item.GetStart()), xy(item.GetEnd()))
        for item in board.GetTracks()
        if item.GetNetname() == cutter_net
        and item.Type() == pcbnew.PCB_TRACE_T
        and item.GetLayer() == plane_layer
    ]


def microvia_obstacles(
    obstacles: list[CopperObstacle], plane_layer: int, bridge_layer: int
) -> list[CopperObstacle]:
    """Return only copper touched by the selected adjacent-layer microvia."""

    layers = {plane_layer, bridge_layer}
    adjacent_pairs = {
        frozenset((pcbnew.F_Cu, pcbnew.In1_Cu)),
        frozenset((pcbnew.In1_Cu, pcbnew.In2_Cu)),
        frozenset((pcbnew.In2_Cu, pcbnew.B_Cu)),
    }
    if frozenset(layers) not in adjacent_pairs:
        # A non-adjacent inner-to-outer transition is implemented as a normal
        # through via, so every copper layer must participate in clearance.
        return list(obstacles)
    result: list[CopperObstacle] = []
    for obstacle in obstacles:
        if obstacle.kind == "pad":
            pad = obstacle.owner
            assert isinstance(pad, pcbnew.PAD)
            if layers.intersection(set(pad.GetLayerSet().Seq())):
                result.append(obstacle)
        elif obstacle.kind == "track":
            if obstacle.geometry[3] in layers:
                result.append(obstacle)
        elif obstacle.kind == "copper_graphic":
            if obstacle.geometry[1] in layers:
                result.append(obstacle)
        elif obstacle.kind == "keepout":
            if layers.intersection(obstacle.geometry[1]):
                result.append(obstacle)
        else:
            # Existing vias can span any layer pair, so keep all of them.
            result.append(obstacle)
    return result


def bridge_candidates(
    *,
    board: pcbnew.BOARD,
    net_name: str,
    cutter_net: str,
    track_width: float,
    maximum_offset: float,
    sample_step: float,
    obstacles: list[CopperObstacle],
    plane_layer: int,
    bridge_layer: int,
) -> list[Bridge]:
    zone = filled_zone(board, net_name, plane_layer)
    polygons = zone.GetFilledPolysList(plane_layer)
    outline_groups = outline_group_map(board, net_name, polygons)
    print(
        f"ZONE {net_name}: outlines={polygons.OutlineCount()} "
        f"mapped={sorted(outline_groups.items())}",
        flush=True,
    )
    for outline_index, group_index in sorted(outline_groups.items()):
        if outline_index > 1:
            continue
        box = polygons.Outline(outline_index).BBox()
        print(
            f"  group={group_index} outline={outline_index} bbox="
            f"{pcbnew.ToMM(box.GetX()):.3f},{pcbnew.ToMM(box.GetY()):.3f},"
            f"{pcbnew.ToMM(box.GetRight()):.3f},{pcbnew.ToMM(box.GetBottom()):.3f}",
            flush=True,
        )
    edge = board_rect(board)
    result: dict[tuple[int, int, int], Bridge] = {}
    open_pairs: dict[
        tuple[int, int],
        list[tuple[float, tuple[float, float], tuple[float, float]]],
    ] = {}
    source_pads: set[str] = set()
    via_obstacles = microvia_obstacles(obstacles, plane_layer, bridge_layer)

    offsets = [
        round(value, 3)
        for value in (
            0.45 + index * 0.10
            for index in range(round((maximum_offset - 0.45) / 0.10) + 1)
        )
    ]
    for segment_start, segment_end in cutter_segments(board, cutter_net, plane_layer):
        dx = segment_end[0] - segment_start[0]
        dy = segment_end[1] - segment_start[1]
        length = math.hypot(dx, dy)
        if length < 0.05:
            continue
        normal = (-dy / length, dx / length)
        sample_count = max(1, math.ceil(length / sample_step))
        for sample_index in range(sample_count + 1):
            fraction = sample_index / sample_count
            center = (
                segment_start[0] + fraction * dx,
                segment_start[1] + fraction * dy,
            )
            side_points: dict[int, list[tuple[float, tuple[float, float], int]]] = {
                -1: [],
                1: [],
            }
            for side in (-1, 1):
                for offset in offsets:
                    candidate = (
                        center[0] + side * normal[0] * offset,
                        center[1] + side * normal[1] * offset,
                    )
                    group_index = group_at(candidate, polygons, outline_groups)
                    if group_index is not None:
                        side_points[side].append((offset, candidate, group_index))
            for _, start, left_group in side_points[-1]:
                if not fanout.via_point_is_clear(
                    net_name=net_name,
                    end=start,
                    source_pads=source_pads,
                    edge=edge,
                    obstacles=via_obstacles,
                ):
                    continue
                for _, end, right_group in side_points[1]:
                    if left_group == right_group:
                        continue
                    if not fanout.via_point_is_clear(
                        net_name=net_name,
                        end=end,
                        source_pads=source_pads,
                        edge=edge,
                        obstacles=via_obstacles,
                    ):
                        continue
                    first, second = sorted((left_group, right_group))
                    ordered_start = start if left_group == first else end
                    ordered_end = end if right_group == second else start
                    open_pairs.setdefault((first, second), []).append(
                        (math.dist(ordered_start, ordered_end), ordered_start, ordered_end)
                    )

    def segment_is_clear(start: tuple[float, float], end: tuple[float, float]) -> bool:
        return fanout.track_segment_is_clear(
            net_name=net_name,
            layer=bridge_layer,
            start=start,
            end=end,
            width_mm=track_width,
            source_pads=source_pads,
            edge=edge,
            obstacles=obstacles,
        )

    # GPIO48 leaves two broad +3V3 regions only 0.405 mm apart near the
    # existing group-3 B.Cu trunk.  Starting on that trunk and landing one
    # In2-to-B microvia in the adjacent filled group-0 polygon avoids the
    # component wall around SW1/R117 without moving either footprint.
    if (
        plane_layer == pcbnew.In2_Cu
        and bridge_layer == pcbnew.B_Cu
        and net_name == "/+3V3"
        and len(disconnected_pad_groups(board, net_name)) == 5
    ):
        reviewed_start = (76.6125, 52.1950)
        reviewed_end = (76.6000, 52.6000)
        reviewed_group = group_at(reviewed_end, polygons, outline_groups)
        if reviewed_group == 0 and segment_is_clear(reviewed_start, reviewed_end):
            result[(0, 3, bridge_layer)] = Bridge(
                net_name,
                0,
                3,
                (reviewed_start, reviewed_end),
                bridge_layer,
                via_start=False,
                via_end=True,
            )

    for pair, options in open_pairs.items():
        unique_options = sorted(
            {
                (
                    round(distance_mm, 6),
                    (round(start[0], 6), round(start[1], 6)),
                    (round(end[0], 6), round(end[1], 6)),
                )
                for distance_mm, start, end in options
            }
        )
        for _, start, end in unique_options:
            paths = ((start, end),)
            doglegs = (
                (start, (start[0], end[1]), end),
                (start, (end[0], start[1]), end),
            )
            paths += doglegs
            for path in paths:
                if not all(
                    segment_is_clear(first, second)
                    for first, second in zip(path, path[1:])
                ):
                    continue
                bridge = Bridge(net_name, pair[0], pair[1], path, bridge_layer)
                key = (pair[0], pair[1], bridge_layer)
                previous = result.get(key)
                if previous is None or bridge.length < previous.length:
                    result[key] = bridge
                break

        key = (pair[0], pair[1], bridge_layer)
        if key in result:
            continue
        # Dense cuts may need a short B.Cu detour.  Group options by their
        # first via site and route to several compatible opposite-side sites
        # in one bounded A* search.
        ends_by_start: dict[tuple[float, float], list[tuple[float, float]]] = {}
        for _, start, end in unique_options:
            ends_by_start.setdefault(start, []).append(end)
        ordered_starts = sorted(
            ends_by_start,
            key=lambda start: min(math.dist(start, end) for end in ends_by_start[start]),
        )
        for start in ordered_starts[:4]:
            ends = tuple(
                sorted(
                    set(ends_by_start[start]),
                    key=lambda end: math.dist(start, end),
                )[:32]
            )
            routed = maze.find_fixed_layer_path_to_goals(
                net_name=net_name,
                start=start,
                ends=ends,
                layer=bridge_layer,
                endpoint_pad_ids=source_pads,
                edge=edge,
                obstacles=obstacles,
                expansion=5.0,
            )
            if routed is None:
                continue
            path, _ = routed
            result[key] = Bridge(net_name, pair[0], pair[1], path, bridge_layer)
            break
    return sorted(
        result.values(),
        key=lambda bridge: (
            bridge.length,
            bridge.left_group,
            bridge.right_group,
            bridge.layer,
        ),
    )


def select_spanning_bridges(group_count: int, candidates: list[Bridge]) -> list[Bridge]:
    parents = list(range(group_count))

    def root(index: int) -> int:
        while parents[index] != index:
            parents[index] = parents[parents[index]]
            index = parents[index]
        return index

    selected: list[Bridge] = []
    for bridge in candidates:
        left = root(bridge.left_group)
        right = root(bridge.right_group)
        if left == right:
            continue
        parents[right] = left
        selected.append(bridge)
        if len(selected) == group_count - 1:
            break
    return selected


def add_bridge(
    board: pcbnew.BOARD,
    bridge: Bridge,
    *,
    track_width: float,
    via_diameter: float,
    via_drill: float,
    obstacles: list[CopperObstacle],
    plane_layer: int,
) -> None:
    net = board.FindNet(bridge.net_name)
    if net is None:
        raise RuntimeError(f"Missing net: {bridge.net_name}")
    via_positions = []
    if bridge.via_start:
        via_positions.append(bridge.start)
    if bridge.via_end:
        via_positions.append(bridge.end)
    for position in via_positions:
        via = pcbnew.PCB_VIA(board)
        via.SetPosition(point(*position))
        via.SetWidth(pcbnew.FromMM(via_diameter))
        via.SetDrill(pcbnew.FromMM(via_drill))
        layer_pair = {plane_layer, bridge.layer}
        if layer_pair == {pcbnew.In1_Cu, pcbnew.In2_Cu}:
            via.SetViaType(pcbnew.VIATYPE_BURIED)
        elif layer_pair in (
            {pcbnew.F_Cu, pcbnew.In1_Cu},
            {pcbnew.In2_Cu, pcbnew.B_Cu},
        ):
            via.SetViaType(pcbnew.VIATYPE_MICROVIA)
        else:
            via.SetViaType(pcbnew.VIATYPE_THROUGH)
            via.SetLayerPair(pcbnew.F_Cu, pcbnew.B_Cu)
        via.SetLayerPair(plane_layer, bridge.layer)
        via.SetNet(net)
        via.SetLocked(True)
        board.Add(via)
        obstacles.append(
            CopperObstacle(
                bridge.net_name,
                "via",
                (position, via_diameter / 2.0),
                via,
            )
        )

    for start, end in zip(bridge.path, bridge.path[1:]):
        if math.dist(start, end) < 0.001:
            continue
        track = pcbnew.PCB_TRACK(board)
        track.SetStart(point(*start))
        track.SetEnd(point(*end))
        track.SetWidth(pcbnew.FromMM(track_width))
        track.SetLayer(bridge.layer)
        track.SetNet(net)
        track.SetLocked(True)
        board.Add(track)
        obstacles.append(
            CopperObstacle(
                bridge.net_name,
                "track",
                (start, end, track_width / 2.0, bridge.layer),
                track,
            )
        )


def main() -> int:
    hardware_dir = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--cutter-net", default="/GPIO48")
    parser.add_argument("--power-net", action="append", required=True)
    parser.add_argument("--track-width", type=float, default=0.20)
    parser.add_argument("--via-diameter", type=float, default=0.30)
    parser.add_argument("--via-drill", type=float, default=0.10)
    parser.add_argument("--clearance", type=float, default=0.20)
    parser.add_argument("--maximum-offset", type=float, default=5.00)
    parser.add_argument("--sample-step", type=float, default=0.25)
    parser.add_argument(
        "--allow-partial",
        action="store_true",
        help="Save the safe spanning bridges found even if not every group can be joined",
    )
    parser.add_argument(
        "--plane-layer",
        choices=("In1.Cu", "In2.Cu", "B.Cu"),
        default="In2.Cu",
    )
    parser.add_argument(
        "--bridge-layer",
        choices=("F.Cu", "In1.Cu", "In2.Cu", "B.Cu"),
        default="B.Cu",
    )
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    output_path = args.output.resolve()
    if output_path == (hardware_dir / "PocketLab-Card.kicad_pcb").resolve():
        raise RuntimeError("Refusing to overwrite the authoritative PCB")
    if output_path.exists() and not args.force:
        raise RuntimeError(f"Output exists; use --force: {output_path}")

    fanout.VIA_DIAMETER_MM = args.via_diameter
    fanout.VIA_DRILL_MM = args.via_drill
    fanout.DIFFERENT_NET_CLEARANCE_MM = args.clearance
    maze.TRACK_WIDTH_MM = args.track_width
    maze.GRID_MM = 0.25
    maze.DIFFERENT_NET_CLEARANCE_MM = args.clearance
    layer_ids = {
        "F.Cu": pcbnew.F_Cu,
        "In1.Cu": pcbnew.In1_Cu,
        "In2.Cu": pcbnew.In2_Cu,
        "B.Cu": pcbnew.B_Cu,
    }
    plane_layer = layer_ids[args.plane_layer]
    bridge_layer = layer_ids[args.bridge_layer]
    adjacent_pairs = {
        frozenset((pcbnew.F_Cu, pcbnew.In1_Cu)),
        frozenset((pcbnew.In1_Cu, pcbnew.In2_Cu)),
        frozenset((pcbnew.In2_Cu, pcbnew.B_Cu)),
    }
    layer_pair = frozenset((plane_layer, bridge_layer))
    through_pairs = {
        frozenset((pcbnew.In1_Cu, pcbnew.B_Cu)),
        frozenset((pcbnew.F_Cu, pcbnew.In2_Cu)),
    }
    if layer_pair not in adjacent_pairs | through_pairs:
        raise RuntimeError(
            "Plane and bridge layers must use an adjacent or inner-to-outer via pair"
        )
    maze.ROUTING_LAYERS = (bridge_layer,)
    board = pcbnew.LoadBoard(str(args.input.resolve()))
    board.BuildConnectivity()
    obstacles = existing_obstacles(board)

    for requested_net in args.power_net:
        net_name = requested_net if requested_net.startswith("/") else f"/{requested_net}"
        fanout.PLANE_LAYER[net_name] = plane_layer
        groups = disconnected_pad_groups(board, net_name)
        if len(groups) < 2:
            print(f"SKIPPED {net_name}: already connected")
            continue
        candidates = bridge_candidates(
            board=board,
            net_name=net_name,
            cutter_net=args.cutter_net,
            track_width=args.track_width,
            maximum_offset=args.maximum_offset,
            sample_step=args.sample_step,
            obstacles=obstacles,
            plane_layer=plane_layer,
            bridge_layer=bridge_layer,
        )
        selected = select_spanning_bridges(len(groups), candidates)
        if len(selected) != len(groups) - 1 and not (args.allow_partial and selected):
            pairs = sorted({(item.left_group, item.right_group) for item in candidates})
            raise RuntimeError(
                f"Could not span {net_name}: groups={len(groups)}, "
                f"selected={len(selected)}, candidate_pairs={pairs}"
            )
        if len(selected) != len(groups) - 1:
            print(
                f"PARTIAL {net_name}: groups={len(groups)}, selected={len(selected)}",
                flush=True,
            )
        for bridge in selected:
            add_bridge(
                board,
                bridge,
                track_width=args.track_width,
                via_diameter=args.via_diameter,
                via_drill=args.via_drill,
                obstacles=obstacles,
                plane_layer=plane_layer,
            )
            print(
                f"BRIDGED {net_name} groups {bridge.left_group}-{bridge.right_group} "
                f"on {board.GetLayerName(bridge.layer)}: "
                f"{bridge.start[0]:.3f},{bridge.start[1]:.3f} -> "
                f"{bridge.end[0]:.3f},{bridge.end[1]:.3f}"
            )
        pcbnew.ZONE_FILLER(board).Fill(board.Zones())
        board.BuildConnectivity()
        remaining = len(disconnected_pad_groups(board, net_name))
        print(f"{net_name}: remaining groups={remaining}")

    pcbnew.SaveBoard(str(output_path), board)
    for suffix in (".kicad_pro", ".kicad_dru"):
        shutil.copyfile(
            hardware_dir / f"PocketLab-Card{suffix}", output_path.with_suffix(suffix)
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
