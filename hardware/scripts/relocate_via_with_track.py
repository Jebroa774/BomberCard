"""Move one via and one attached track endpoint in a candidate board."""

from __future__ import annotations

import argparse
from pathlib import Path
import shutil

import pcbnew


def uuid_text(item: pcbnew.BOARD_ITEM) -> str:
    value = item.m_Uuid
    return value.AsString() if hasattr(value, "AsString") else str(value)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--via-uuid", required=True)
    parser.add_argument("--track-uuid", action="append", required=True)
    parser.add_argument("--x", type=float, required=True)
    parser.add_argument("--y", type=float, required=True)
    parser.add_argument(
        "--fanout-layer", choices=("F.Cu", "B.Cu", "In1.Cu", "In2.Cu")
    )
    parser.add_argument("--fanout-width", type=float, default=0.15)
    parser.add_argument("--skip-fill-zones", action="store_true")
    parser.add_argument(
        "--allow-opens",
        action="store_true",
        help="save diagnostic outputs even when the relocation creates opens",
    )
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    hardware_dir = Path(__file__).resolve().parent.parent
    authoritative = (hardware_dir / "PocketLab-Card.kicad_pcb").resolve()
    if args.output.resolve() in {authoritative, args.input.resolve()}:
        raise RuntimeError("output must be a separate non-authoritative board")
    if args.output.exists() and not args.force:
        raise RuntimeError(f"output exists: {args.output}")

    board = pcbnew.LoadBoard(str(args.input.resolve()))
    by_uuid = {uuid_text(item): item for item in board.GetTracks()}
    via = by_uuid.get(args.via_uuid)
    tracks = [by_uuid.get(item_uuid) for item_uuid in args.track_uuid]
    if not isinstance(via, pcbnew.PCB_VIA):
        raise RuntimeError("via UUID does not select a via")
    if any(track is None or isinstance(track, pcbnew.PCB_VIA) for track in tracks):
        raise RuntimeError("track UUID does not select a track")
    if any(via.GetNetCode() != track.GetNetCode() for track in tracks):
        raise RuntimeError("via and track are on different nets")

    old = via.GetPosition()
    new = pcbnew.VECTOR2I_MM(args.x, args.y)
    for track in tracks:
        if track.GetStart() == old:
            track.SetStart(new)
        elif track.GetEnd() == old:
            track.SetEnd(new)
        else:
            raise RuntimeError("selected track does not end at selected via")
    via.SetPosition(new)

    if args.fanout_layer:
        layer_by_name = {
            "F.Cu": pcbnew.F_Cu,
            "B.Cu": pcbnew.B_Cu,
            "In1.Cu": pcbnew.In1_Cu,
            "In2.Cu": pcbnew.In2_Cu,
        }
        fanout = pcbnew.PCB_TRACK(board)
        fanout.SetStart(old)
        fanout.SetEnd(new)
        fanout.SetLayer(layer_by_name[args.fanout_layer])
        fanout.SetWidth(pcbnew.FromMM(args.fanout_width))
        fanout.SetNet(via.GetNet())
        board.Add(fanout)

    if not args.skip_fill_zones:
        pcbnew.ZONE_FILLER(board).Fill(board.Zones())
    board.BuildConnectivity()
    connectivity = board.GetConnectivity()
    connectivity.RecalculateRatsnest()
    opens = int(connectivity.GetUnconnectedCount(False))
    if opens and not args.allow_opens:
        raise RuntimeError(f"via relocation created {opens} open connection(s)")

    pcbnew.SaveBoard(str(args.output.resolve()), board)
    for suffix in (".kicad_pro", ".kicad_dru"):
        shutil.copyfile(
            hardware_dir / f"PocketLab-Card{suffix}", args.output.with_suffix(suffix)
        )
    print(
        f"MOVED via={args.via_uuid} tracks={len(tracks)} "
        f"to=({args.x:.4f},{args.y:.4f}) opens={opens}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
