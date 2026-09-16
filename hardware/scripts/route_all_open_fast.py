"""Connect every DRC-reported open edge in one deliberately fast batch.

This is a working-candidate tool, not an acceptance router.  It preserves the
authoritative PCB, adds real net-assigned copper for every current ratsnest
edge, refills zones once, and leaves clearance/short cleanup to one subsequent
KiCad DRC pass.  The workflow is useful when connectivity progress is more
important than checking each individual route while it is being created.
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import re
import shutil

import pcbnew


NET_RE = re.compile(r"\[([^\]]+)\]")
LAYER_RE = re.compile(r"\bauf\s+(F\.Cu|B\.Cu|GND|PWR)\b")
LAYER_IDS = {
    "F.Cu": pcbnew.F_Cu,
    "B.Cu": pcbnew.B_Cu,
    "GND": pcbnew.In1_Cu,
    "PWR": pcbnew.In2_Cu,
}
POWER_NETS = {
    "/GND",
    "/+3V3",
    "/+5V_AUX",
    "/+5V_RAW",
    "/VSYS",
    "/VBUS_USB",
    "/VBUS_FUSED",
    "/CELL_POS",
    "/BAT_FET_MID",
}


def endpoint_layers(description: str) -> set[int]:
    if description.startswith("Durchsteckpad") or "F.Cu - B.Cu" in description:
        return {pcbnew.F_Cu, pcbnew.In1_Cu, pcbnew.In2_Cu, pcbnew.B_Cu}
    match = LAYER_RE.search(description)
    if match is None:
        return set()
    return {LAYER_IDS[match.group(1)]}


def point(item: dict) -> tuple[float, float]:
    return float(item["pos"]["x"]), float(item["pos"]["y"])


def add_track(
    board: pcbnew.BOARD,
    net: pcbnew.NETINFO_ITEM,
    layer: int,
    width_mm: float,
    start: tuple[float, float],
    end: tuple[float, float],
) -> int:
    if math.dist(start, end) < 0.001:
        return 0
    track = pcbnew.PCB_TRACK(board)
    track.SetStart(pcbnew.VECTOR2I_MM(*start))
    track.SetEnd(pcbnew.VECTOR2I_MM(*end))
    track.SetWidth(pcbnew.FromMM(width_mm))
    track.SetLayer(layer)
    track.SetNet(net)
    track.SetLocked(True)
    board.Add(track)
    return 1


def add_layer_via(
    board: pcbnew.BOARD,
    net: pcbnew.NETINFO_ITEM,
    position: tuple[float, float],
    endpoint_layers: set[int],
    route_layer: int,
    diameter_mm: float,
    drill_mm: float,
) -> None:
    copper_order = (pcbnew.F_Cu, pcbnew.In1_Cu, pcbnew.In2_Cu, pcbnew.B_Cu)
    endpoint_layer = min(
        endpoint_layers,
        key=lambda layer: abs(copper_order.index(layer) - copper_order.index(route_layer)),
    )
    layer_pair = {endpoint_layer, route_layer}
    via = pcbnew.PCB_VIA(board)
    via.SetPosition(pcbnew.VECTOR2I_MM(*position))
    via.SetWidth(pcbnew.FromMM(diameter_mm))
    via.SetDrill(pcbnew.FromMM(drill_mm))
    if layer_pair in (
        {pcbnew.F_Cu, pcbnew.In1_Cu},
        {pcbnew.In2_Cu, pcbnew.B_Cu},
    ):
        via.SetViaType(pcbnew.VIATYPE_MICROVIA)
        via.SetLayerPair(endpoint_layer, route_layer)
    elif layer_pair == {pcbnew.In1_Cu, pcbnew.In2_Cu}:
        via.SetViaType(pcbnew.VIATYPE_BURIED)
        via.SetLayerPair(endpoint_layer, route_layer)
    elif layer_pair in (
        {pcbnew.F_Cu, pcbnew.In2_Cu},
        {pcbnew.In1_Cu, pcbnew.B_Cu},
    ):
        via.SetViaType(pcbnew.VIATYPE_BLIND)
        via.SetLayerPair(endpoint_layer, route_layer)
    else:
        via.SetViaType(pcbnew.VIATYPE_THROUGH)
        via.SetLayerPair(pcbnew.F_Cu, pcbnew.B_Cu)
    via.SetNet(net)
    via.SetLocked(True)
    board.Add(via)


def choose_layer(first: set[int], second: set[int], index: int) -> int:
    common = first & second
    if pcbnew.F_Cu in common:
        return pcbnew.F_Cu
    if pcbnew.B_Cu in common:
        return pcbnew.B_Cu
    # Mismatched SMD/inner endpoints need a via anyway.  Alternating outer
    # layers distributes the intentionally unchecked first-pass copper.
    return pcbnew.F_Cu if index % 2 == 0 else pcbnew.B_Cu


def choose_inner_layer(first: set[int], second: set[int], index: int) -> int:
    # Prefer the inner layer directly adjacent to SMD endpoints so most fanout
    # vias are legal one-step microvias instead of plane-cutting through vias.
    if first == {pcbnew.F_Cu} and second == {pcbnew.F_Cu}:
        return pcbnew.In1_Cu
    if first == {pcbnew.B_Cu} and second == {pcbnew.B_Cu}:
        return pcbnew.In2_Cu
    return pcbnew.In1_Cu if index % 2 == 0 else pcbnew.In2_Cu


def route_points(
    start: tuple[float, float],
    end: tuple[float, float],
    layer: int,
    index: int,
) -> tuple[tuple[float, float], ...]:
    if layer in (pcbnew.In1_Cu, pcbnew.In2_Cu):
        return (start, end)
    distance = math.dist(start, end)
    if distance <= 28.0:
        return (start, end)
    # Long routes use distributed dogleg lanes instead of all piling through
    # the board centre.  Exact collision cleanup is intentionally deferred.
    if layer == pcbnew.F_Cu:
        lane_y = 20.0 + ((index * 1.37) % 50.0)
        return (start, (start[0], lane_y), (end[0], lane_y), end)
    lane_x = 22.0 + ((index * 1.73) % 80.0)
    return (start, (lane_x, start[1]), (lane_x, end[1]), end)


def main() -> int:
    hardware_dir = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--drc", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--signal-width", type=float, default=0.15)
    parser.add_argument("--power-width", type=float, default=0.40)
    parser.add_argument("--via-diameter", type=float, default=0.45)
    parser.add_argument("--via-drill", type=float, default=0.20)
    parser.add_argument(
        "--routing-mode",
        choices=(
            "outer",
            "outer-direct",
            "all-f-direct",
            "all-b-direct",
            "mapped-direct",
            "inner",
        ),
        default="outer",
    )
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--net", action="append")
    parser.add_argument("--front-net", action="append")
    parser.add_argument("--back-net", action="append")
    parser.add_argument(
        "--skip-zone-fill",
        action="store_true",
        help="Preserve the candidate's current zone fill while adding batch routes",
    )
    args = parser.parse_args()

    output = args.output.resolve()
    authoritative = (hardware_dir / "PocketLab-Card.kicad_pcb").resolve()
    if output == authoritative:
        raise RuntimeError("Refusing to overwrite the authoritative PCB")
    if output.exists() and not args.force:
        raise RuntimeError(f"Output exists; use --force: {output}")

    board = pcbnew.LoadBoard(str(args.input.resolve()))
    report = json.loads(args.drc.read_text(encoding="utf-8"))
    selected_nets = set(args.net or ())
    front_nets = set(args.front_net or ())
    back_nets = set(args.back_net or ())
    if front_nets & back_nets:
        raise RuntimeError("A net cannot be forced to both outer layers")
    routed = 0
    tracks = 0
    vias = 0
    skipped = 0
    for index, entry in enumerate(report.get("unconnected_items", [])):
        items = entry.get("items", [])
        if len(items) != 2:
            skipped += 1
            continue
        match = NET_RE.search(items[0].get("description", ""))
        if match is None:
            skipped += 1
            continue
        net_name = match.group(1)
        if selected_nets and net_name not in selected_nets:
            continue
        net = board.FindNet(net_name)
        if net is None:
            skipped += 1
            continue
        first_layers = endpoint_layers(items[0].get("description", ""))
        second_layers = endpoint_layers(items[1].get("description", ""))
        if not first_layers or not second_layers:
            skipped += 1
            continue
        start = point(items[0])
        end = point(items[1])
        if args.routing_mode == "inner":
            layer = choose_inner_layer(first_layers, second_layers, index)
        elif args.routing_mode == "mapped-direct" and net_name in front_nets:
            layer = pcbnew.F_Cu
        elif args.routing_mode == "mapped-direct" and net_name in back_nets:
            layer = pcbnew.B_Cu
        elif args.routing_mode == "all-f-direct":
            layer = pcbnew.F_Cu
        elif args.routing_mode == "all-b-direct":
            layer = pcbnew.B_Cu
        else:
            layer = choose_layer(first_layers, second_layers, index)
        if layer not in first_layers:
            add_layer_via(
                board, net, start, first_layers, layer,
                args.via_diameter, args.via_drill
            )
            vias += 1
        if layer not in second_layers:
            add_layer_via(
                board, net, end, second_layers, layer,
                args.via_diameter, args.via_drill
            )
            vias += 1
        width = args.power_width if net_name in POWER_NETS else args.signal_width
        path = (
            (start, end)
            if args.routing_mode in {
                "outer-direct",
                "all-f-direct",
                "all-b-direct",
                "mapped-direct",
            }
            else route_points(start, end, layer, index)
        )
        tracks += sum(
            add_track(board, net, layer, width, first, second)
            for first, second in zip(path, path[1:])
        )
        routed += 1

    if not args.skip_zone_fill:
        pcbnew.ZONE_FILLER(board).Fill(board.Zones())
    output.parent.mkdir(parents=True, exist_ok=True)
    pcbnew.SaveBoard(str(output), board)
    for suffix in (".kicad_pro", ".kicad_dru"):
        shutil.copyfile(
            hardware_dir / f"PocketLab-Card{suffix}", output.with_suffix(suffix)
        )
    reloaded = pcbnew.LoadBoard(str(output))
    reloaded.BuildConnectivity()
    connectivity = reloaded.GetConnectivity()
    connectivity.RecalculateRatsnest()
    print(
        f"SAVED routed_edges={routed} skipped={skipped} tracks={tracks} "
        f"vias={vias} opens={int(connectivity.GetUnconnectedCount(False))}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
