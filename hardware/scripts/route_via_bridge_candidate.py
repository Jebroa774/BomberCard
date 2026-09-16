"""Add one explicit through-via bridge to a separate PCB candidate."""

from __future__ import annotations

import argparse
from pathlib import Path
import re

import pcbnew


def parse_point(value: str) -> tuple[float, float]:
    x_text, y_text = value.split(",", 1)
    return float(x_text), float(y_text)


def add_via(
    board: pcbnew.BOARD,
    net_name: str,
    point: tuple[float, float],
    via_kind: str,
) -> list[pcbnew.PCB_VIA]:
    if via_kind == "buried-in1-in2":
        return [add_buried_in1_in2_via(board, net_name, point)]
    if via_kind == "stack-b-in1":
        return add_via(board, net_name, point, "microvia-b-in2") + [
            add_buried_in1_in2_via(board, net_name, point)
        ]
    template = next(
        (
            item
            for item in board.GetTracks()
            if item.Type() == pcbnew.PCB_VIA_T and item.GetNetname() == net_name
        ),
        None,
    )
    via = template.Duplicate() if template is not None else pcbnew.PCB_VIA(board)
    via.SetPosition(pcbnew.VECTOR2I_MM(*point))
    if via_kind == "microvia-f-in1":
        via.SetViaType(pcbnew.VIATYPE_MICROVIA)
        via.SetWidth(pcbnew.FromMM(0.30))
        via.SetDrill(pcbnew.FromMM(0.10))
        via.SetLayerPair(pcbnew.F_Cu, pcbnew.In1_Cu)
    elif via_kind == "microvia-b-in2":
        via.SetViaType(pcbnew.VIATYPE_MICROVIA)
        via.SetWidth(pcbnew.FromMM(0.30))
        via.SetDrill(pcbnew.FromMM(0.10))
        via.SetLayerPair(pcbnew.In2_Cu, pcbnew.B_Cu)
    elif via_kind == "blind-b-in1":
        via.SetViaType(pcbnew.VIATYPE_BLIND)
        via.SetWidth(pcbnew.FromMM(0.30))
        via.SetDrill(pcbnew.FromMM(0.10))
        via.SetLayerPair(pcbnew.In1_Cu, pcbnew.B_Cu)
    else:
        via.SetViaType(pcbnew.VIATYPE_THROUGH)
        via.SetWidth(pcbnew.FromMM(0.45))
        via.SetDrill(pcbnew.FromMM(0.20))
        via.SetLayerPair(pcbnew.F_Cu, pcbnew.B_Cu)
    if template is None:
        via.SetNetCode(board.FindNet(net_name).GetNetCode())
    via.SetLocked(True)
    board.Add(via)
    return [via]


def add_track(
    board: pcbnew.BOARD,
    net_name: str,
    start: tuple[float, float],
    end: tuple[float, float],
    width_mm: float,
    layer: int,
) -> pcbnew.PCB_TRACK:
    template = next(
        (
            item
            for item in board.GetTracks()
            if item.Type() == pcbnew.PCB_TRACE_T and item.GetNetname() == net_name
        ),
        None,
    )
    track = template.Duplicate() if template is not None else pcbnew.PCB_TRACK(board)
    track.SetStart(pcbnew.VECTOR2I_MM(*start))
    track.SetEnd(pcbnew.VECTOR2I_MM(*end))
    track.SetWidth(pcbnew.FromMM(width_mm))
    track.SetLayer(layer)
    if template is None:
        track.SetNetCode(board.FindNet(net_name).GetNetCode())
    track.SetLocked(True)
    board.Add(track)
    return track


def add_buried_in1_in2_via(
    board: pcbnew.BOARD,
    net_name: str,
    point: tuple[float, float],
) -> pcbnew.PCB_VIA:
    template = next(
        (
            item
            for item in board.GetTracks()
            if item.Type() == pcbnew.PCB_VIA_T and item.GetNetname() == net_name
        ),
        None,
    )
    via = template.Duplicate() if template is not None else pcbnew.PCB_VIA(board)
    via.SetPosition(pcbnew.VECTOR2I_MM(*point))
    # This board's DRC-clean stackup represents every adjacent-layer
    # transition as a 0.30/0.10 mm microvia, including In1.Cu-In2.Cu.
    via.SetViaType(pcbnew.VIATYPE_MICROVIA)
    via.SetWidth(pcbnew.FromMM(0.30))
    via.SetDrill(pcbnew.FromMM(0.10))
    via.SetLayerPair(pcbnew.In1_Cu, pcbnew.In2_Cu)
    if template is None:
        via.SetNetCode(board.FindNet(net_name).GetNetCode())
    via.SetLocked(True)
    board.Add(via)
    return via


def repair_saved_item_nets(
    output_path: Path,
    item_nets: dict[str, str],
) -> None:
    """Work around KiCad 10 Python SaveBoard remapping newly added item netcodes."""

    text = output_path.read_text(encoding="utf-8")
    for uuid, net_name in item_nets.items():
        marker = f'(uuid "{uuid}")'
        marker_at = text.find(marker)
        if marker_at < 0:
            raise RuntimeError(f"Saved item UUID is absent: {uuid}")
        via_at = text.rfind("\n\t(via", 0, marker_at)
        segment_at = text.rfind("\n\t(segment", 0, marker_at)
        block_at = max(via_at, segment_at)
        block_end = text.find("\n\t)", marker_at)
        if block_at < 0 or block_end < 0:
            raise RuntimeError(f"Cannot locate saved item block: {uuid}")
        block_end += len("\n\t)")
        block = text[block_at:block_end]
        replaced, count = re.subn(
            r'\n\t\t\(net "[^"]*"\)',
            f'\n\t\t(net "{net_name}")',
            block,
            count=1,
        )
        if count != 1:
            raise RuntimeError(f"Cannot repair saved item net: {uuid}")
        text = text[:block_at] + replaced + text[block_end:]
    output_path.write_text(text, encoding="utf-8", newline="\n")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--net", required=True)
    parser.add_argument("--point", action="append", type=parse_point, required=True)
    parser.add_argument("--pad-start", type=parse_point)
    parser.add_argument("--pad-end", type=parse_point)
    parser.add_argument(
        "--layer",
        choices=("F.Cu", "In1.Cu", "In2.Cu", "B.Cu"),
        default="F.Cu",
    )
    parser.add_argument(
        "--via",
        choices=(
            "through",
            "microvia-f-in1",
            "microvia-b-in2",
            "blind-b-in1",
            "stack-b-in1",
            "buried-in1-in2",
        ),
        default="through",
    )
    parser.add_argument("--width", type=float, default=0.20)
    parser.add_argument(
        "--via-endpoints",
        choices=("both", "start", "end", "none"),
        default="both",
        help="which route endpoints receive the requested via stack",
    )
    parser.add_argument("--restore-net")
    parser.add_argument("--restore-point", action="append", type=parse_point)
    parser.add_argument("--restore-width", type=float, default=0.50)
    parser.add_argument(
        "--skip-zone-fill",
        action="store_true",
        help="preserve the input board's stored zone fill in the candidate",
    )
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    if len(args.point) < 1:
        raise RuntimeError("At least one --point argument is required")
    if args.input.resolve() == args.output.resolve():
        raise RuntimeError("Output must be separate from input")
    if args.output.exists() and not args.force:
        raise RuntimeError(f"Output exists; use --force: {args.output}")

    board = pcbnew.LoadBoard(str(args.input.resolve()))
    if board.FindNet(args.net) is None:
        raise RuntimeError(f"Unknown net: {args.net}")
    route_layer = {
        "F.Cu": pcbnew.F_Cu,
        "In1.Cu": pcbnew.In1_Cu,
        "In2.Cu": pcbnew.In2_Cu,
        "B.Cu": pcbnew.B_Cu,
    }[args.layer]
    if args.via == "microvia-b-in2" and route_layer not in {pcbnew.In2_Cu, pcbnew.B_Cu}:
        raise RuntimeError("microvia-b-in2 requires an In2.Cu or B.Cu bridge track")
    if args.via == "blind-b-in1" and route_layer != pcbnew.In1_Cu:
        raise RuntimeError("blind-b-in1 requires --layer In1.Cu")
    if args.via == "microvia-f-in1" and route_layer not in {pcbnew.F_Cu, pcbnew.In1_Cu}:
        raise RuntimeError("microvia-f-in1 requires an F.Cu or In1.Cu bridge track")
    if args.via == "stack-b-in1" and route_layer not in {pcbnew.In1_Cu, pcbnew.B_Cu}:
        raise RuntimeError("stack-b-in1 requires --layer In1.Cu or B.Cu")
    if args.via == "buried-in1-in2" and route_layer not in {pcbnew.In1_Cu, pcbnew.In2_Cu}:
        raise RuntimeError("buried-in1-in2 requires --layer In1.Cu or In2.Cu")
    item_nets: dict[str, str] = {}

    def remember(items: list[pcbnew.BOARD_ITEM], net_name: str) -> None:
        for item in items:
            item_nets[str(item.m_Uuid.AsString())] = net_name
            print(f"ADDED {item.Type()} {item.GetNetname()} {item.m_Uuid.AsString()}")

    if args.via_endpoints in {"both", "start"}:
        remember(add_via(board, args.net, args.point[0], args.via), args.net)
    if args.via_endpoints in {"both", "end"}:
        remember(add_via(board, args.net, args.point[-1], args.via), args.net)
    if args.pad_start:
        remember(
            [add_track(board, args.net, args.pad_start, args.point[0], args.width, pcbnew.B_Cu)],
            args.net,
        )
    if args.pad_end:
        remember(
            [add_track(board, args.net, args.point[-1], args.pad_end, args.width, pcbnew.B_Cu)],
            args.net,
        )
    for start, end in zip(args.point, args.point[1:]):
        remember([add_track(board, args.net, start, end, args.width, route_layer)], args.net)

    if args.restore_net or args.restore_point:
        if not args.restore_net or not args.restore_point or len(args.restore_point) != 2:
            raise RuntimeError(
                "Zone restoration requires --restore-net and exactly two --restore-point values"
            )
        if board.FindNet(args.restore_net) is None:
            raise RuntimeError(f"Unknown restoration net: {args.restore_net}")
        for point in args.restore_point:
            remember([add_buried_in1_in2_via(board, args.restore_net, point)], args.restore_net)
        remember(
            [
                add_track(
                    board,
                    args.restore_net,
                    args.restore_point[0],
                    args.restore_point[1],
                    args.restore_width,
                    pcbnew.In1_Cu,
                )
            ],
            args.restore_net,
        )

    if not args.skip_zone_fill:
        if not pcbnew.ZONE_FILLER(board).Fill(board.Zones()):
            raise RuntimeError("Zone refill failed")
    pcbnew.SaveBoard(str(args.output.resolve()), board)
    reloaded = pcbnew.LoadBoard(str(args.output.resolve()))
    reloaded.BuildConnectivity()
    connectivity = reloaded.GetConnectivity()
    connectivity.RecalculateRatsnest()
    print(
        f"Saved bridge candidate: net={args.net}, segments={len(args.point) - 1}, "
        f"opens={int(connectivity.GetUnconnectedCount(False))}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
