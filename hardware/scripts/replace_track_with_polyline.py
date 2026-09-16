"""Replace one candidate track by a same-layer polyline without touching the base."""

from __future__ import annotations

import argparse
from pathlib import Path
import shutil

import pcbnew


def uuid_text(item: pcbnew.BOARD_ITEM) -> str:
    value = item.m_Uuid
    return value.AsString() if hasattr(value, "AsString") else str(value)


def parse_point(value: str) -> tuple[float, float]:
    pieces = value.split(",")
    if len(pieces) != 2:
        raise argparse.ArgumentTypeError("point must be X,Y in millimetres")
    return float(pieces[0]), float(pieces[1])


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--uuid", required=True)
    parser.add_argument("--point", action="append", type=parse_point, required=True)
    parser.add_argument("--layer", choices=("F.Cu", "In1.Cu", "In2.Cu", "B.Cu"))
    parser.add_argument("--width", type=float)
    parser.add_argument("--fill-zones", action="store_true")
    parser.add_argument("--require-zero-open", action="store_true")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    if args.output.resolve() == args.input.resolve():
        raise RuntimeError("output must differ from input")
    if args.output.exists() and not args.force:
        raise RuntimeError(f"output exists: {args.output}")
    board = pcbnew.LoadBoard(str(args.input.resolve()))
    matches = [item for item in board.GetTracks() if uuid_text(item) == args.uuid]
    if len(matches) != 1:
        raise RuntimeError(f"expected one track UUID {args.uuid}, got {len(matches)}")
    original = matches[0]
    if isinstance(original, pcbnew.PCB_VIA):
        raise RuntimeError("target UUID is a via")
    points = [
        (
            pcbnew.ToMM(original.GetStart().x),
            pcbnew.ToMM(original.GetStart().y),
        ),
        *args.point,
        (
            pcbnew.ToMM(original.GetEnd().x),
            pcbnew.ToMM(original.GetEnd().y),
        ),
    ]
    net = original.GetNet()
    layer = board.GetLayerID(args.layer) if args.layer else original.GetLayer()
    width = (
        pcbnew.FromMM(args.width)
        if args.width is not None
        else original.GetWidth()
    )
    locked = original.IsLocked()
    board.Remove(original)
    added = 0
    for start, end in zip(points, points[1:]):
        if abs(start[0] - end[0]) < 0.001 and abs(start[1] - end[1]) < 0.001:
            continue
        track = pcbnew.PCB_TRACK(board)
        track.SetStart(pcbnew.VECTOR2I_MM(*start))
        track.SetEnd(pcbnew.VECTOR2I_MM(*end))
        track.SetLayer(layer)
        track.SetWidth(width)
        track.SetNet(net)
        track.SetLocked(locked)
        board.Add(track)
        added += 1
    if args.fill_zones:
        pcbnew.ZONE_FILLER(board).Fill(board.Zones())
    board.BuildConnectivity()
    connectivity = board.GetConnectivity()
    connectivity.RecalculateRatsnest()
    opens = int(connectivity.GetUnconnectedCount(False))
    if args.require_zero_open and opens:
        raise RuntimeError(f"replacement created {opens} open connection(s)")
    pcbnew.SaveBoard(str(args.output.resolve()), board)
    hardware_dir = Path(__file__).resolve().parent.parent
    for suffix in (".kicad_pro", ".kicad_dru"):
        shutil.copyfile(hardware_dir / f"PocketLab-Card{suffix}", args.output.with_suffix(suffix))
    print(
        f"REPLACED {args.uuid} net={net.GetNetname()} "
        f"layer={board.GetLayerName(layer)} segments={added} opens={opens}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
