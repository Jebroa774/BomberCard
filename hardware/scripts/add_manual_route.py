"""Add an explicitly reviewed polyline route to a KiCad PCB candidate."""

from __future__ import annotations

import argparse
from pathlib import Path
import shutil

import pcbnew


def parse_point(value: str) -> tuple[float, float]:
    try:
        x, y = value.split(",", 1)
        return float(x), float(y)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("point must be X,Y in millimetres") from exc


def parse_via(value: str) -> tuple[float, float, str, str, str, float, float]:
    try:
        x, y, top, bottom, kind, diameter, drill = value.split(",", 6)
        return float(x), float(y), top, bottom, kind, float(diameter), float(drill)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            "via must be X,Y,TOP,BOTTOM,TYPE,DIAMETER,DRILL"
        ) from exc


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--net", required=True)
    parser.add_argument("--layer", default="F.Cu")
    parser.add_argument("--width", type=float, default=0.15)
    parser.add_argument("--point", action="append", type=parse_point, default=[])
    parser.add_argument("--via", action="append", type=parse_via, default=[])
    parser.add_argument("--fill-zones", action="store_true")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    if args.point and len(args.point) < 2:
        parser.error("provide either no points or at least two --point arguments")
    if not args.point and not args.via:
        parser.error("provide at least one route segment or via")
    if args.output.exists() and not args.force:
        parser.error(f"output exists: {args.output}")

    board = pcbnew.LoadBoard(str(args.input))
    net_info = board.FindNet(args.net)
    if net_info is None:
        parser.error(f"net not found: {args.net}")
    # Keep the integer code.  NETINFO_ITEM proxies returned by KiCad's SWIG
    # name map are transient and may be reused for a later lookup.
    net_code = net_info.GetNetCode()
    if board.FindNet(net_code).GetNetname() != args.net:
        parser.error(f"net lookup mismatch for {args.net}: code {net_code}")
    layer = board.GetLayerID(args.layer)
    if layer == pcbnew.UNDEFINED_LAYER:
        parser.error(f"layer not found: {args.layer}")

    for start, end in zip(args.point, args.point[1:]):
        track = pcbnew.PCB_TRACK(board)
        track.SetStart(pcbnew.VECTOR2I_MM(*start))
        track.SetEnd(pcbnew.VECTOR2I_MM(*end))
        track.SetWidth(pcbnew.FromMM(args.width))
        track.SetLayer(layer)
        track.SetNetCode(net_code)
        board.Add(track)

    via_types = {
        "through": pcbnew.VIATYPE_THROUGH,
        "microvia": pcbnew.VIATYPE_MICROVIA,
        "buried": pcbnew.VIATYPE_BURIED,
        "blind": pcbnew.VIATYPE_BLIND,
    }
    for x, y, top_name, bottom_name, kind, diameter, drill in args.via:
        if kind not in via_types:
            parser.error(f"unknown via type: {kind}")
        top = board.GetLayerID(top_name)
        bottom = board.GetLayerID(bottom_name)
        if top == pcbnew.UNDEFINED_LAYER or bottom == pcbnew.UNDEFINED_LAYER:
            parser.error(f"invalid via layer pair: {top_name},{bottom_name}")
        via = pcbnew.PCB_VIA(board)
        via.SetPosition(pcbnew.VECTOR2I_MM(x, y))
        via.SetWidth(pcbnew.FromMM(diameter))
        via.SetDrill(pcbnew.FromMM(drill))
        via.SetViaType(via_types[kind])
        via.SetLayerPair(top, bottom)
        via.SetNetCode(net_code)
        board.Add(via)

    if args.fill_zones:
        pcbnew.ZONE_FILLER(board).Fill(board.Zones())

    pcbnew.SaveBoard(str(args.output), board)
    hardware_dir = Path(__file__).resolve().parent.parent
    for suffix in (".kicad_pro", ".kicad_dru"):
        shutil.copyfile(hardware_dir / f"PocketLab-Card{suffix}", args.output.with_suffix(suffix))
    print(
        f"Saved {args.output}: {len(args.point) - 1} segments, "
        f"{len(args.via)} vias on {args.net}"
    )


if __name__ == "__main__":
    main()
