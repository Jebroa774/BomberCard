"""Replace one existing track with a fixed-layer obstacle-aware A* path."""

from __future__ import annotations

import argparse
from pathlib import Path
import shutil

import pcbnew

import route_lf_global as maze
from route_plane_fanouts import CopperObstacle, board_rect, existing_obstacles


def uuid_text(item: pcbnew.BOARD_ITEM) -> str:
    value = item.m_Uuid
    return value.AsString() if hasattr(value, "AsString") else str(value)


def xy(position: pcbnew.VECTOR2I) -> tuple[float, float]:
    return pcbnew.ToMM(position.x), pcbnew.ToMM(position.y)


def avoid_point(value: str) -> tuple[float, float, float]:
    x, y, radius = value.split(",", 2)
    return float(x), float(y), float(radius)


def main() -> int:
    hardware_dir = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--uuid", required=True)
    parser.add_argument(
        "--layer", choices=("F.Cu", "In1.Cu", "In2.Cu", "B.Cu"), required=True
    )
    parser.add_argument("--grid", type=float, default=0.15)
    parser.add_argument("--clearance", type=float, default=0.20)
    parser.add_argument("--expansion", type=float, default=20.0)
    parser.add_argument("--pad-obstacles-only", action="store_true")
    parser.add_argument(
        "--static-obstacles-only",
        action="store_true",
        help="avoid pads, vias and keepouts while allowing later crossing cleanup",
    )
    parser.add_argument("--ignore-endpoint-cages", action="store_true")
    parser.add_argument(
        "--avoid-point",
        action="append",
        type=avoid_point,
        default=[],
        help="reserve x,y,radius millimetres as a same-layer circular obstacle",
    )
    parser.add_argument("--fill-zones", action="store_true")
    parser.add_argument("--require-zero-open", action="store_true")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    output = args.output.resolve()
    authoritative = (hardware_dir / "PocketLab-Card.kicad_pcb").resolve()
    if output in {authoritative, args.input.resolve()}:
        raise RuntimeError("output must be a separate non-authoritative board")
    if output.exists() and not args.force:
        raise RuntimeError(f"output exists: {output}")

    board = pcbnew.LoadBoard(str(args.input.resolve()))
    matches = [item for item in board.GetTracks() if uuid_text(item) == args.uuid]
    if len(matches) != 1 or isinstance(matches[0], pcbnew.PCB_VIA):
        raise RuntimeError(f"UUID must select exactly one track: {args.uuid}")
    original = matches[0]
    start, end = xy(original.GetStart()), xy(original.GetEnd())
    net_name = original.GetNetname()
    width_mm = pcbnew.ToMM(original.GetWidth())
    locked = original.IsLocked()
    board.Remove(original)

    obstacles = existing_obstacles(board)
    routing_obstacles = [
        obstacle
        for obstacle in obstacles
        if obstacle.net != net_name
        and (
            not (args.pad_obstacles_only or args.static_obstacles_only)
            or obstacle.kind in {"pad", "keepout", "copper_graphic"}
            or (args.static_obstacles_only and obstacle.kind == "via")
        )
    ]
    routing_obstacles.extend(
        CopperObstacle("/__REROUTE_KEEPOUT__", "via", ((x, y), radius))
        for x, y, radius in args.avoid_point
    )
    maze.GRID_MM = args.grid
    maze.TRACK_WIDTH_MM = width_mm
    maze.DIFFERENT_NET_CLEARANCE_MM = args.clearance
    maze.AVOID_L3_ZONE_POLYS = ()
    if args.ignore_endpoint_cages:
        routing_obstacles = [
            obstacle
            for obstacle in routing_obstacles
            if obstacle.net == "/__REROUTE_KEEPOUT__"
            or (
                not maze.obstacle_rect(obstacle)
                .expanded(args.clearance + width_mm / 2.0)
                .contains(start)
                and not maze.obstacle_rect(obstacle)
                .expanded(args.clearance + width_mm / 2.0)
                .contains(end)
            )
        ]
    layer = board.GetLayerID(args.layer)
    path = maze.find_fixed_layer_path(
        net_name=net_name,
        start=start,
        end=end,
        layer=layer,
        endpoint_pad_ids=set(),
        edge=board_rect(board),
        obstacles=routing_obstacles,
        expansion=args.expansion,
    )
    if path is None:
        print("FAILED no path")
        return 2

    # The grid router keeps every change between horizontal and diagonal
    # movement.  With off-grid endpoints this can produce a long staircase
    # even though several consecutive steps have a clear direct line of sight.
    # Greedily retain the farthest reachable point so the saved PCB contains
    # a small number of clean segments rather than dozens of 0.2 mm fragments.
    if len(path) > 2:
        simplified = [path[0]]
        anchor = 0
        edge = board_rect(board)
        while anchor < len(path) - 1:
            for candidate in range(len(path) - 1, anchor, -1):
                if maze.track_segment_is_clear(
                    net_name=net_name,
                    layer=layer,
                    start=path[anchor],
                    end=path[candidate],
                    width_mm=width_mm,
                    source_pads=set(),
                    edge=edge,
                    obstacles=routing_obstacles,
                ):
                    simplified.append(path[candidate])
                    anchor = candidate
                    break
            else:
                raise RuntimeError("could not preserve a clear A* path segment")
        path = tuple(simplified)

    route = tuple((x, y, layer) for x, y in path)
    tracks, vias = maze.add_route(board, net_name, route, obstacles)
    for item in board.GetTracks():
        if item.GetNetname() == net_name and not isinstance(item, pcbnew.PCB_VIA):
            # Preserve the reviewed lock state on newly created path sections.
            if any(
                item.GetStart() == pcbnew.VECTOR2I_MM(*point[:2])
                or item.GetEnd() == pcbnew.VECTOR2I_MM(*point[:2])
                for point in route
            ):
                item.SetLocked(locked)
    if args.fill_zones:
        pcbnew.ZONE_FILLER(board).Fill(board.Zones())
    board.BuildConnectivity()
    connectivity = board.GetConnectivity()
    connectivity.RecalculateRatsnest()
    opens = int(connectivity.GetUnconnectedCount(False))
    if args.require_zero_open and opens:
        raise RuntimeError(f"replacement created {opens} open connection(s)")
    pcbnew.SaveBoard(str(output), board)
    for suffix in (".kicad_pro", ".kicad_dru"):
        shutil.copyfile(
            hardware_dir / f"PocketLab-Card{suffix}", output.with_suffix(suffix)
        )
    print(
        f"REROUTED {args.uuid} net={net_name} layer={args.layer} "
        f"tracks={tracks} vias={vias} points={len(path)} opens={opens}"
    )
    print("PATH " + " ".join(f"{x:.4f},{y:.4f}" for x, y in path))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
