"""Route a reviewed copper-to-copper connection between explicit points.

This helper is intentionally candidate-only.  It is useful when a DRC open
item points at a caged component pad although an accessible piece of the same
net already exists nearby.
"""

from __future__ import annotations

import argparse
from pathlib import Path
import shutil

import pcbnew

import route_lf_global as maze
from route_plane_fanouts import board_rect, existing_obstacles


def parse_point(value: str) -> tuple[float, float]:
    try:
        x, y = value.split(",", 1)
        return float(x), float(y)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("point must be X,Y in millimetres") from exc


def main() -> int:
    hardware_dir = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--net", required=True)
    parser.add_argument("--start", type=parse_point, required=True)
    parser.add_argument("--end", type=parse_point, required=True)
    parser.add_argument(
        "--layer",
        choices=("F.Cu", "In1.Cu", "In2.Cu", "B.Cu"),
        required=True,
    )
    parser.add_argument(
        "--start-layer",
        choices=("F.Cu", "In1.Cu", "In2.Cu", "B.Cu"),
        help="copper layer of the start anchor; adds a via when it differs from --layer",
    )
    parser.add_argument(
        "--end-layer",
        choices=("F.Cu", "In1.Cu", "In2.Cu", "B.Cu"),
        help="copper layer of the end anchor; adds a via when it differs from --layer",
    )
    parser.add_argument("--grid", type=float, default=0.15)
    parser.add_argument("--width", type=float, default=0.20)
    parser.add_argument("--clearance", type=float, default=0.20)
    parser.add_argument("--via-diameter", type=float, default=0.30)
    parser.add_argument("--via-drill", type=float, default=0.10)
    parser.add_argument("--expansion", type=float, default=16.0)
    parser.add_argument("--pad-obstacles-only", action="store_true")
    parser.add_argument(
        "--ignore-endpoint-cages",
        action="store_true",
        help="ignore conservative obstacle halos already containing an endpoint",
    )
    parser.add_argument("--fill-zones", action="store_true")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    output = args.output.resolve()
    authoritative = (hardware_dir / "PocketLab-Card.kicad_pcb").resolve()
    if output == authoritative:
        raise RuntimeError("Refusing to overwrite the authoritative PCB")
    if output.exists() and not args.force:
        raise RuntimeError(f"Output exists; use --force: {output}")

    board = pcbnew.LoadBoard(str(args.input.resolve()))
    if board.FindNet(args.net) is None:
        raise RuntimeError(f"Net not found: {args.net}")
    layer = board.GetLayerID(args.layer)
    obstacles = existing_obstacles(board)
    routing_obstacles = [
        obstacle
        for obstacle in obstacles
        if obstacle.net != args.net
        and (
            not args.pad_obstacles_only
            or obstacle.kind in {"pad", "keepout", "copper_graphic"}
        )
    ]
    maze.GRID_MM = args.grid
    maze.TRACK_WIDTH_MM = args.width
    maze.DIFFERENT_NET_CLEARANCE_MM = args.clearance
    maze.VIA_DIAMETER_MM = args.via_diameter
    maze.VIA_DRILL_MM = args.via_drill
    maze.AVOID_L3_ZONE_POLYS = ()
    if args.ignore_endpoint_cages:
        routing_obstacles = [
            obstacle
            for obstacle in routing_obstacles
            if not maze.obstacle_rect(obstacle)
            .expanded(args.clearance + args.width / 2.0)
            .contains(args.start)
            and not maze.obstacle_rect(obstacle)
            .expanded(args.clearance + args.width / 2.0)
            .contains(args.end)
        ]
    path = maze.find_fixed_layer_path(
        net_name=args.net,
        start=args.start,
        end=args.end,
        layer=layer,
        endpoint_pad_ids=set(),
        edge=board_rect(board),
        obstacles=routing_obstacles,
        expansion=args.expansion,
    )
    if path is None:
        print("FAILED no path")
        return 2
    route_entries = [(x, y, layer) for x, y in path]
    if args.start_layer:
        start_layer = board.GetLayerID(args.start_layer)
        if start_layer != layer:
            route_entries.insert(0, (args.start[0], args.start[1], start_layer))
    if args.end_layer:
        end_layer = board.GetLayerID(args.end_layer)
        if end_layer != layer:
            route_entries.append((args.end[0], args.end[1], end_layer))
    route = tuple(route_entries)
    tracks, vias = maze.add_route(board, args.net, route, obstacles)
    if args.fill_zones:
        pcbnew.ZONE_FILLER(board).Fill(board.Zones())
    pcbnew.SaveBoard(str(output), board)
    for suffix in (".kicad_pro", ".kicad_dru"):
        shutil.copyfile(
            hardware_dir / f"PocketLab-Card{suffix}", output.with_suffix(suffix)
        )
    print(f"SAVED tracks={tracks} vias={vias} points={len(path)}")
    print("PATH " + " ".join(f"{x:.4f},{y:.4f}" for x, y in path))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
