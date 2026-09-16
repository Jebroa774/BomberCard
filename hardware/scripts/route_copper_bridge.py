"""Route one DRC-aware bridge between two existing copper islands."""

from __future__ import annotations

import argparse
from pathlib import Path
import shutil

import pcbnew

import route_lf_global as maze
from route_plane_fanouts import board_rect, existing_obstacles


def parse_point(value: str) -> tuple[float, float]:
    x_text, y_text = value.split(",", 1)
    return float(x_text), float(y_text)


def main() -> int:
    hardware_dir = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--net", required=True)
    parser.add_argument("--start", type=parse_point, required=True)
    parser.add_argument("--end", type=parse_point, required=True)
    parser.add_argument(
        "--start-via",
        type=parse_point,
        help="optional displaced via position; a native-layer escape joins --start",
    )
    parser.add_argument(
        "--end-via",
        type=parse_point,
        help="optional displaced via position; a native-layer escape joins --end",
    )
    parser.add_argument(
        "--start-escape-waypoint",
        type=parse_point,
        action="append",
        default=[],
        help="optional native-layer waypoint between --start and --start-via; repeatable",
    )
    parser.add_argument(
        "--end-escape-waypoint",
        type=parse_point,
        action="append",
        default=[],
        help="optional native-layer waypoint between --end and --end-via; repeatable",
    )
    parser.add_argument(
        "--layer", choices=("F.Cu", "In1.Cu", "In2.Cu", "B.Cu"), default="B.Cu"
    )
    parser.add_argument(
        "--endpoint-vias",
        choices=("none", "F-B", "F-In1", "In1-B", "In1-In2", "In2-B"),
        default="none",
    )
    parser.add_argument(
        "--via-endpoints",
        choices=("both", "start", "end"),
        default="both",
        help="which bridge endpoints receive --endpoint-vias",
    )
    parser.add_argument("--via-diameter", type=float, default=0.30)
    parser.add_argument("--via-drill", type=float, default=0.10)
    parser.add_argument("--grid", type=float, default=0.20)
    parser.add_argument("--width", type=float, default=0.20)
    parser.add_argument("--clearance", type=float, default=0.20)
    parser.add_argument("--expansion", type=float, default=12.0)
    parser.add_argument(
        "--ignore-endpoint-cages",
        action="store_true",
        help="ignore pre-existing obstacle halos that already contain start or end",
    )
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    output = args.output.resolve()
    if output == (hardware_dir / "PocketLab-Card.kicad_pcb").resolve():
        raise RuntimeError("Refusing to overwrite the authoritative PCB")
    if output.exists() and not args.force:
        raise RuntimeError(f"Output exists; use --force: {output}")

    board = pcbnew.LoadBoard(str(args.input.resolve()))
    net_name = args.net if args.net.startswith("/") else f"/{args.net}"
    if board.FindNet(net_name) is None:
        raise RuntimeError(f"Unknown net: {net_name}")
    layer = {
        "F.Cu": pcbnew.F_Cu,
        "In1.Cu": pcbnew.In1_Cu,
        "In2.Cu": pcbnew.In2_Cu,
        "B.Cu": pcbnew.B_Cu,
    }[args.layer]
    maze.GRID_MM = args.grid
    maze.TRACK_WIDTH_MM = args.width
    maze.DIFFERENT_NET_CLEARANCE_MM = args.clearance
    maze.AVOID_L3_ZONE_POLYS = ()
    maze.ROUTING_LAYERS = (layer,)
    bridge_start = args.start_via or args.start
    bridge_end = args.end_via or args.end
    # A bridge intentionally starts and ends on existing copper of its own
    # net.  The generic fixed-layer maze applies same-net spacing to vias and
    # tracks as well, which otherwise cages the search at an existing via or
    # track endpoint.  Unrelated copper remains fully obstacle-checked; the
    # final KiCad DRC still validates the completed candidate.
    obstacles = [
        obstacle
        for obstacle in existing_obstacles(board)
        if obstacle.net != net_name
    ]
    if args.ignore_endpoint_cages:
        def obstacle_cages_endpoint(obstacle: maze.CopperObstacle) -> bool:
            endpoints = (bridge_start, bridge_end)
            route_radius = args.clearance + args.width / 2.0
            if obstacle.kind == "track":
                obstacle_start, obstacle_end, radius, _ = obstacle.geometry
                return any(
                    radius + 1e-6
                    < maze.point_segment_distance(endpoint, obstacle_start, obstacle_end)
                    <= radius + route_radius + 1e-6
                    for endpoint in endpoints
                )
            if obstacle.kind == "pad":
                pad_rect = obstacle.geometry
                return any(
                    not pad_rect.contains(endpoint)
                    and pad_rect.expanded(route_radius).contains(endpoint)
                    for endpoint in endpoints
                )
            if obstacle.kind == "via":
                center, radius = obstacle.geometry
                return any(
                    radius + 1e-6
                    < maze.distance(endpoint, center)
                    <= radius + route_radius + 1e-6
                    for endpoint in endpoints
                )
            return False

        obstacles = [
            obstacle
            for obstacle in obstacles
            if not obstacle_cages_endpoint(obstacle)
        ]
    result = maze.find_fixed_layer_path_to_goals(
        net_name=net_name,
        start=bridge_start,
        ends=(bridge_end,),
        layer=layer,
        endpoint_pad_ids=set(),
        edge=board_rect(board),
        obstacles=obstacles,
        expansion=args.expansion,
    )
    if result is None:
        raise RuntimeError("No clear copper bridge path found")
    points, _ = result
    route = tuple((x, y, layer) for x, y in points)
    tracks, vias = maze.add_route(board, net_name, route, obstacles)
    via_pairs = {
        "F-B": (pcbnew.F_Cu, pcbnew.B_Cu, pcbnew.VIATYPE_THROUGH),
        "F-In1": (pcbnew.F_Cu, pcbnew.In1_Cu, pcbnew.VIATYPE_MICROVIA),
        "In1-B": (pcbnew.In1_Cu, pcbnew.B_Cu, pcbnew.VIATYPE_BLIND),
        "In1-In2": (pcbnew.In1_Cu, pcbnew.In2_Cu, pcbnew.VIATYPE_BURIED),
        "In2-B": (pcbnew.In2_Cu, pcbnew.B_Cu, pcbnew.VIATYPE_MICROVIA),
    }
    if args.endpoint_vias != "none":
        top, bottom, via_type = via_pairs[args.endpoint_vias]
        net = board.FindNet(net_name)
        assert net is not None
        native_layers = {top, bottom}.difference({layer})
        if len(native_layers) != 1:
            raise RuntimeError("Endpoint-via pair must include the selected route layer")
        native_layer = next(iter(native_layers))
        endpoint_specs = {
            "start": (args.start, bridge_start, args.start_escape_waypoint),
            "end": (args.end, bridge_end, args.end_escape_waypoint),
        }
        selected_endpoints = (
            ("start", "end")
            if args.via_endpoints == "both"
            else (args.via_endpoints,)
        )
        for endpoint_name in selected_endpoints:
            endpoint, position, escape_waypoints = endpoint_specs[endpoint_name]
            if maze.distance(endpoint, position) > 0.001:
                escape_points = (
                    endpoint,
                    *escape_waypoints,
                    position,
                )
                escape = tuple((x, y, native_layer) for x, y in escape_points)
                added_tracks, added_vias = maze.add_route(
                    board, net_name, escape, obstacles
                )
                tracks += added_tracks
                vias += added_vias
            via = pcbnew.PCB_VIA(board)
            via.SetPosition(pcbnew.VECTOR2I_MM(*position))
            via.SetWidth(pcbnew.FromMM(args.via_diameter))
            via.SetDrill(pcbnew.FromMM(args.via_drill))
            via.SetViaType(via_type)
            via.SetLayerPair(top, bottom)
            via.SetNet(net)
            via.SetLocked(True)
            board.Add(via)
            vias += 1
    pcbnew.ZONE_FILLER(board).Fill(board.Zones())
    pcbnew.SaveBoard(str(output), board)
    for suffix in (".kicad_pro", ".kicad_dru"):
        shutil.copyfile(hardware_dir / f"PocketLab-Card{suffix}", output.with_suffix(suffix))
    print(f"ROUTED {net_name}: tracks={tracks} vias={vias} points={len(points)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
