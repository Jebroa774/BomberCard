"""Bridge candidate-added crossing tracks onto the opposite outer layer."""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import shutil

import pcbnew

import route_lf_global as maze
from route_plane_fanouts import board_rect, existing_obstacles


def geometry_key(item: pcbnew.BOARD_ITEM) -> tuple:
    if isinstance(item, pcbnew.PCB_VIA):
        return (
            "via",
            item.GetPosition().x,
            item.GetPosition().y,
            item.GetWidth(pcbnew.F_Cu),
            item.GetDrillValue(),
            int(item.GetViaType()),
            item.TopLayer(),
            item.BottomLayer(),
            item.GetNetname(),
        )
    first = (item.GetStart().x, item.GetStart().y)
    second = (item.GetEnd().x, item.GetEnd().y)
    if second < first:
        first, second = second, first
    return (
        "track",
        first,
        second,
        item.GetWidth(),
        item.GetLayer(),
        item.GetNetname(),
    )


def uuid_text(item: pcbnew.BOARD_ITEM) -> str:
    value = item.m_Uuid
    return value.AsString() if hasattr(value, "AsString") else str(value)


def xy(position: pcbnew.VECTOR2I) -> tuple[float, float]:
    return pcbnew.ToMM(position.x), pcbnew.ToMM(position.y)


def intersection_parameter(
    first_start: tuple[float, float],
    first_end: tuple[float, float],
    second_start: tuple[float, float],
    second_end: tuple[float, float],
) -> tuple[float, float, tuple[float, float]] | None:
    rx = first_end[0] - first_start[0]
    ry = first_end[1] - first_start[1]
    sx = second_end[0] - second_start[0]
    sy = second_end[1] - second_start[1]
    denominator = rx * sy - ry * sx
    if abs(denominator) < 1e-9:
        return None
    qx = second_start[0] - first_start[0]
    qy = second_start[1] - first_start[1]
    first_t = (qx * sy - qy * sx) / denominator
    second_t = (qx * ry - qy * rx) / denominator
    if not (0.0 < first_t < 1.0 and 0.0 < second_t < 1.0):
        return None
    point = (first_start[0] + first_t * rx, first_start[1] + first_t * ry)
    return first_t, second_t, point


def add_track(
    board: pcbnew.BOARD,
    net: pcbnew.NETINFO_ITEM,
    layer: int,
    width: int,
    start: tuple[float, float],
    end: tuple[float, float],
) -> None:
    if math.dist(start, end) < 0.001:
        return
    track = pcbnew.PCB_TRACK(board)
    track.SetStart(pcbnew.VECTOR2I_MM(*start))
    track.SetEnd(pcbnew.VECTOR2I_MM(*end))
    track.SetLayer(layer)
    track.SetWidth(width)
    track.SetNet(net)
    track.SetLocked(True)
    board.Add(track)


def add_path(
    board: pcbnew.BOARD,
    net: pcbnew.NETINFO_ITEM,
    layer: int,
    width: int,
    path: tuple[tuple[float, float], ...],
) -> None:
    for start, end in zip(path, path[1:]):
        add_track(board, net, layer, width, start, end)


def add_via(
    board: pcbnew.BOARD,
    net: pcbnew.NETINFO_ITEM,
    position: tuple[float, float],
    first_layer: int,
    second_layer: int,
) -> None:
    via = pcbnew.PCB_VIA(board)
    via.SetPosition(pcbnew.VECTOR2I_MM(*position))
    via.SetWidth(pcbnew.FromMM(maze.VIA_DIAMETER_MM))
    via.SetDrill(pcbnew.FromMM(maze.VIA_DRILL_MM))
    inner_pair = {first_layer, second_layer} == {pcbnew.In1_Cu, pcbnew.In2_Cu}
    via.SetViaType(pcbnew.VIATYPE_MICROVIA if inner_pair else pcbnew.VIATYPE_THROUGH)
    via.SetLayerPair(
        min(first_layer, second_layer) if inner_pair else pcbnew.F_Cu,
        max(first_layer, second_layer) if inner_pair else pcbnew.B_Cu,
    )
    via.SetNet(net)
    via.SetLocked(True)
    board.Add(via)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", type=Path, required=True)
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--drc", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--max-bridges", type=int, default=20)
    parser.add_argument("--via-diameter", type=float, default=0.45)
    parser.add_argument("--via-drill", type=float, default=0.20)
    parser.add_argument(
        "--allow-existing-victim",
        action="store_true",
        help="permit bridging the roomier pre-existing track at a crossing",
    )
    parser.add_argument(
        "--prefer-existing-victim",
        action="store_true",
        help="when possible, bridge the pre-existing track instead of candidate copper",
    )
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    if args.output.resolve() in {args.base.resolve(), args.candidate.resolve()}:
        raise RuntimeError("output must differ from base and candidate")
    if args.output.exists() and not args.force:
        raise RuntimeError(f"output exists: {args.output}")

    base = pcbnew.LoadBoard(str(args.base.resolve()))
    board = pcbnew.LoadBoard(str(args.candidate.resolve()))
    base_keys = {geometry_key(item) for item in base.GetTracks()}
    new_uuids = {
        uuid_text(item)
        for item in board.GetTracks()
        if geometry_key(item) not in base_keys
    }
    report = json.loads(args.drc.read_text(encoding="utf-8"))
    maze.VIA_DIAMETER_MM = args.via_diameter
    maze.VIA_DRILL_MM = args.via_drill
    edge = board_rect(board)
    bridges = 0
    failed = 0

    for violation in report.get("violations", []):
        if bridges >= args.max_bridges:
            break
        if violation.get("type") != "tracks_crossing":
            continue
        item_uuids = [entry.get("uuid", "") for entry in violation.get("items", [])]
        current_by_uuid = {uuid_text(item): item for item in board.GetTracks()}
        tracks = [current_by_uuid.get(item_uuid) for item_uuid in item_uuids]
        if len(tracks) != 2 or any(item is None for item in tracks):
            continue
        first, second = tracks
        if isinstance(first, pcbnew.PCB_VIA) or isinstance(second, pcbnew.PCB_VIA):
            continue
        routing_layers = (
            pcbnew.F_Cu,
            pcbnew.In1_Cu,
            pcbnew.In2_Cu,
            pcbnew.B_Cu,
        )
        if (
            first.GetLayer() != second.GetLayer()
            or first.GetLayer() not in routing_layers
        ):
            continue
        candidates = [
            item
            for item in (first, second)
            if not isinstance(item, pcbnew.PCB_VIA)
            and (
                args.allow_existing_victim
                or uuid_text(item) in new_uuids
            )
        ]
        if not candidates:
            continue
        # Prefer bridging the longer newly added segment because it provides
        # more room to place both transition vias away from the crossing.
        existing_candidates = [
            item for item in candidates if uuid_text(item) not in new_uuids
        ]
        if args.prefer_existing_victim and existing_candidates:
            victim = max(existing_candidates, key=lambda item: item.GetLength())
        else:
            victim = max(candidates, key=lambda item: item.GetLength())
        other = second if victim is first else first
        start = xy(victim.GetStart())
        end = xy(victim.GetEnd())
        other_start = xy(other.GetStart())
        other_end = xy(other.GetEnd())
        result = intersection_parameter(start, end, other_start, other_end)
        if result is None:
            continue
        parameter, _, crossing = result
        length = math.dist(start, end)
        before = parameter * length
        after = (1.0 - parameter) * length
        if before < 0.28 or after < 0.28:
            continue
        direction = ((end[0] - start[0]) / length, (end[1] - start[1]) / length)
        perpendicular = (-direction[1], direction[0])
        original_layer = victim.GetLayer()
        # Prefer an outer signal layer for bridges that originate on an inner
        # plane.  A through-via is larger than a blind transition, but it keeps
        # the short bridge away from the dense inner-layer track bundle and is
        # fully checked across every copper layer below.
        if original_layer == pcbnew.F_Cu:
            bridge_layers = (pcbnew.B_Cu,)
        elif original_layer == pcbnew.B_Cu:
            bridge_layers = (pcbnew.F_Cu,)
        elif original_layer == pcbnew.In1_Cu:
            bridge_layers = (pcbnew.B_Cu, pcbnew.F_Cu, pcbnew.In2_Cu)
        elif original_layer == pcbnew.In2_Cu:
            bridge_layers = (pcbnew.F_Cu, pcbnew.B_Cu, pcbnew.In1_Cu)
        else:
            bridge_layers = ()
        obstacles = existing_obstacles(board)
        routing_obstacles = [obstacle for obstacle in obstacles if obstacle.net != victim.GetNetname()]
        spatial = maze.SpatialIndex(routing_obstacles)
        chosen = None
        for bridge_layer in bridge_layers:
            for distance in (0.30, 0.45, 0.60, 0.70, 0.90, 1.20, 1.60, 2.20, 3.00, 4.00):
                if distance >= before - 0.05 or distance >= after - 0.05:
                    continue
                nominal_first = (
                    crossing[0] - direction[0] * distance,
                    crossing[1] - direction[1] * distance,
                )
                nominal_second = (
                    crossing[0] + direction[0] * distance,
                    crossing[1] + direction[1] * distance,
                )
                offsets = (0.0, 0.35, -0.35, 0.60, -0.60, 0.90, -0.90, 1.20, -1.20)
                # Trying equal offsets first gives a tidy parallel bridge.  The
                # full pair search then permits a slight diagonal transition when
                # only one side of the crossing has local via room.
                offset_pairs = [(offset, offset) for offset in offsets]
                offset_pairs.extend(
                    (first_offset, second_offset)
                    for first_offset in offsets
                    for second_offset in offsets
                    if first_offset != second_offset
                )
                for first_offset, second_offset in offset_pairs:
                    first_via = (
                        nominal_first[0] + perpendicular[0] * first_offset,
                        nominal_first[1] + perpendicular[1] * first_offset,
                    )
                    second_via = (
                        nominal_second[0] + perpendicular[0] * second_offset,
                        nominal_second[1] + perpendicular[1] * second_offset,
                    )
                    via_layers = (
                        {original_layer, bridge_layer}
                        if {original_layer, bridge_layer} == {pcbnew.In1_Cu, pcbnew.In2_Cu}
                        else {pcbnew.F_Cu, pcbnew.In1_Cu, pcbnew.In2_Cu, pcbnew.B_Cu}
                    )
                    if not maze.signal_via_is_clear(
                        net_name=victim.GetNetname(),
                        position=first_via,
                        endpoint_pad_ids=set(),
                        edge=edge,
                        obstacles=spatial.query_point(first_via),
                        via_layers=via_layers,
                    ):
                        continue
                    if not maze.signal_via_is_clear(
                        net_name=victim.GetNetname(),
                        position=second_via,
                        endpoint_pad_ids=set(),
                        edge=edge,
                        obstacles=spatial.query_point(second_via),
                        via_layers=via_layers,
                    ):
                        continue
                    # Offset vias require two new approach segments on the
                    # original layer; validate those before spending time on A*.
                    if first_offset and not maze.track_segment_is_clear(
                        net_name=victim.GetNetname(),
                        layer=original_layer,
                        start=start,
                        end=first_via,
                        width_mm=pcbnew.ToMM(victim.GetWidth()),
                        source_pads=set(),
                        edge=edge,
                        obstacles=spatial.query_segment(start, first_via),
                    ):
                        continue
                    if second_offset and not maze.track_segment_is_clear(
                        net_name=victim.GetNetname(),
                        layer=original_layer,
                        start=second_via,
                        end=end,
                        width_mm=pcbnew.ToMM(victim.GetWidth()),
                        source_pads=set(),
                        edge=edge,
                        obstacles=spatial.query_segment(second_via, end),
                    ):
                        continue
                    if maze.track_segment_is_clear(
                        net_name=victim.GetNetname(),
                        layer=bridge_layer,
                        start=first_via,
                        end=second_via,
                        width_mm=pcbnew.ToMM(victim.GetWidth()),
                        source_pads=set(),
                        edge=edge,
                        obstacles=spatial.query_segment(first_via, second_via),
                    ):
                        bridge_path = (first_via, second_via)
                    else:
                        # In dense areas a very short A* dogleg on another
                        # copper layer can clear the obstruction.
                        maze.TRACK_WIDTH_MM = pcbnew.ToMM(victim.GetWidth())
                        bridge_path = maze.find_fixed_layer_path(
                            net_name=victim.GetNetname(),
                            start=first_via,
                            end=second_via,
                            layer=bridge_layer,
                            endpoint_pad_ids=set(),
                            edge=edge,
                            obstacles=routing_obstacles,
                            expansion=min(4.0, max(1.5, distance + 0.5)),
                        )
                        if bridge_path is None:
                            continue
                    chosen = first_via, second_via, tuple(bridge_path), bridge_layer
                    break
                if chosen is not None:
                    break
            if chosen is not None:
                break
        if chosen is None:
            failed += 1
            continue

        first_via, second_via, bridge_path, bridge_layer = chosen
        net = victim.GetNet()
        width = victim.GetWidth()
        board.Remove(victim)
        add_track(board, net, original_layer, width, start, first_via)
        add_via(board, net, first_via, original_layer, bridge_layer)
        add_path(board, net, bridge_layer, width, bridge_path)
        add_via(board, net, second_via, original_layer, bridge_layer)
        add_track(board, net, original_layer, width, second_via, end)
        bridges += 1
        print(
            f"BRIDGED {net.GetNetname()} {board.GetLayerName(original_layer)}->"
            f"{board.GetLayerName(bridge_layer)} at {crossing[0]:.3f},{crossing[1]:.3f}",
            flush=True,
        )

    pcbnew.SaveBoard(str(args.output.resolve()), board)
    hardware_dir = Path(__file__).resolve().parent.parent
    for suffix in (".kicad_pro", ".kicad_dru"):
        shutil.copyfile(hardware_dir / f"PocketLab-Card{suffix}", args.output.with_suffix(suffix))
    print(f"SAVED bridges={bridges} failed={failed}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
