"""Find short legal outer-layer microvia bridges across an inner-layer cut."""

from __future__ import annotations

import argparse
import math
from pathlib import Path

import pcbnew

from autoroute_in1_candidate import mm, segment_clear_on_layer


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--cutter-net", required=True)
    parser.add_argument("--plane-net", default="/GND")
    parser.add_argument(
        "--layer",
        choices=("In1.Cu", "In2.Cu", "B.Cu"),
        default="In1.Cu",
        help="plane layer to bridge (In1 uses F.Cu; In2 uses B.Cu; B uses In2.Cu)",
    )
    parser.add_argument(
        "--bridge-layer",
        choices=("F.Cu", "In1.Cu", "In2.Cu", "B.Cu"),
        help="override the layer carrying the bridge track",
    )
    parser.add_argument("--sample-step", type=float, default=0.40)
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument(
        "--geometry-only",
        action="store_true",
        help="report polygon-spanning candidates without copper-clearance filtering",
    )
    parser.add_argument(
        "--show-outline-pairs",
        action="store_true",
        help="append filled-polygon outline indices for each endpoint",
    )
    args = parser.parse_args()

    board = pcbnew.LoadBoard(str(args.input.resolve()))
    plane_layer = {
        "In1.Cu": pcbnew.In1_Cu,
        "In2.Cu": pcbnew.In2_Cu,
        "B.Cu": pcbnew.B_Cu,
    }[args.layer]
    bridge_layer = {
        pcbnew.In1_Cu: pcbnew.F_Cu,
        pcbnew.In2_Cu: pcbnew.B_Cu,
        pcbnew.B_Cu: pcbnew.In2_Cu,
    }[plane_layer]
    if args.bridge_layer:
        bridge_layer = {
            "F.Cu": pcbnew.F_Cu,
            "In1.Cu": pcbnew.In1_Cu,
            "In2.Cu": pcbnew.In2_Cu,
            "B.Cu": pcbnew.B_Cu,
        }[args.bridge_layer]
    zone = next(
        zone
        for zone in board.Zones()
        if zone.GetNetname() == args.plane_net
        and zone.HasFilledPolysForLayer(plane_layer)
    )
    polygons = zone.GetFilledPolysList(plane_layer)
    found = []
    offsets = (0.55, 0.65, 0.75, 0.90, 1.10, 1.30, 1.50, 1.80, 2.10)
    for track in board.GetTracks():
        if (
            track.Type() != pcbnew.PCB_TRACE_T
            or track.GetNetname() != args.cutter_net
            or track.GetLayer() != plane_layer
        ):
            continue
        start = mm(track.GetStart().x), mm(track.GetStart().y)
        end = mm(track.GetEnd().x), mm(track.GetEnd().y)
        dx, dy = end[0] - start[0], end[1] - start[1]
        length = math.hypot(dx, dy)
        if length < 0.50:
            continue
        normal = -dy / length, dx / length
        samples = max(1, int(length / args.sample_step))
        for index in range(samples + 1):
            fraction = index / samples
            center = start[0] + fraction * dx, start[1] + fraction * dy
            for offset in offsets:
                left = center[0] - normal[0] * offset, center[1] - normal[1] * offset
                right = center[0] + normal[0] * offset, center[1] + normal[1] * offset
                if not (
                    polygons.Collide(pcbnew.VECTOR2I_MM(*left))
                    and polygons.Collide(pcbnew.VECTOR2I_MM(*right))
                ):
                    continue
                if args.geometry_only:
                    found.append((math.dist(left, right), left, right))
                    continue
                if any(
                    not segment_clear_on_layer(
                        board, layer, args.plane_net, left, left, item_radius=0.15
                    )
                    for layer in (bridge_layer, plane_layer)
                ):
                    continue
                if any(
                    not segment_clear_on_layer(
                        board, layer, args.plane_net, right, right, item_radius=0.15
                    )
                    for layer in (bridge_layer, plane_layer)
                ):
                    continue
                if not segment_clear_on_layer(
                    board, bridge_layer, args.plane_net, left, right
                ):
                    continue
                found.append((math.dist(left, right), left, right))

    unique = sorted(
        {
            (
                round(length, 6),
                (round(left[0], 6), round(left[1], 6)),
                (round(right[0], 6), round(right[1], 6)),
            )
            for length, left, right in found
        }
    )
    for length, left, right in unique[: args.limit]:
        pair = ""
        if args.show_outline_pairs:
            left_ids = [
                index
                for index in range(polygons.OutlineCount())
                if polygons.COutline(index).PointInside(pcbnew.VECTOR2I_MM(*left))
            ]
            right_ids = [
                index
                for index in range(polygons.OutlineCount())
                if polygons.COutline(index).PointInside(pcbnew.VECTOR2I_MM(*right))
            ]
            pair = f" outlines={left_ids}->{right_ids}"
        print(
            f"{length:.3f} {left[0]:.3f},{left[1]:.3f} -> "
            f"{right[0]:.3f},{right[1]:.3f}{pair}"
        )
    print(f"FOUND {len(unique)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
