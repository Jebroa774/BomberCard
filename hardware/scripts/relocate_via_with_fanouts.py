"""Move a via while preserving existing tracks and bridge old/new nodes by layer."""

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
    parser.add_argument("--x", type=float, required=True)
    parser.add_argument("--y", type=float, required=True)
    parser.add_argument(
        "--fanout-layer",
        action="append",
        choices=("F.Cu", "B.Cu", "In1.Cu", "In2.Cu"),
    )
    parser.add_argument("--fanout-width", type=float, default=0.15)
    parser.add_argument(
        "--rewire-connected",
        action="store_true",
        help="move track endpoints that touched the old via instead of adding fanouts",
    )
    parser.add_argument("--skip-fill-zones", action="store_true")
    parser.add_argument(
        "--allow-existing-opens",
        action="store_true",
        help="permit the board's pre-existing unrouted connections",
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
    matches = [
        item
        for item in board.GetTracks()
        if isinstance(item, pcbnew.PCB_VIA) and uuid_text(item) == args.via_uuid
    ]
    if len(matches) != 1:
        raise RuntimeError(f"expected one via UUID {args.via_uuid}, got {len(matches)}")
    via = matches[0]
    old = via.GetPosition()
    new = pcbnew.VECTOR2I_MM(args.x, args.y)

    rewired = 0
    if args.rewire_connected:
        for item in board.GetTracks():
            if isinstance(item, pcbnew.PCB_VIA) or item.GetNetCode() != via.GetNetCode():
                continue
            if item.GetStart() == old:
                item.SetStart(new)
                rewired += 1
            if item.GetEnd() == old:
                item.SetEnd(new)
                rewired += 1
    via.SetPosition(new)

    layer_by_name = {
        "F.Cu": pcbnew.F_Cu,
        "B.Cu": pcbnew.B_Cu,
        "In1.Cu": pcbnew.In1_Cu,
        "In2.Cu": pcbnew.In2_Cu,
    }
    fanout_layers = list(dict.fromkeys(args.fanout_layer or []))
    if args.rewire_connected and fanout_layers:
        raise RuntimeError("--rewire-connected cannot be combined with --fanout-layer")
    for layer_name in fanout_layers:
        fanout = pcbnew.PCB_TRACK(board)
        fanout.SetStart(old)
        fanout.SetEnd(new)
        fanout.SetLayer(layer_by_name[layer_name])
        fanout.SetWidth(pcbnew.FromMM(args.fanout_width))
        fanout.SetNet(via.GetNet())
        fanout.SetLocked(True)
        board.Add(fanout)

    if not args.skip_fill_zones:
        pcbnew.ZONE_FILLER(board).Fill(board.Zones())
    board.BuildConnectivity()
    connectivity = board.GetConnectivity()
    connectivity.RecalculateRatsnest()
    opens = int(connectivity.GetUnconnectedCount(False))
    if opens and not args.allow_existing_opens:
        raise RuntimeError(f"via relocation created {opens} open connection(s)")

    pcbnew.SaveBoard(str(args.output.resolve()), board)
    for suffix in (".kicad_pro", ".kicad_dru"):
        shutil.copyfile(
            hardware_dir / f"PocketLab-Card{suffix}", args.output.with_suffix(suffix)
        )
    print(
        f"MOVED via={args.via_uuid} to=({args.x:.4f},{args.y:.4f}) "
        f"fanouts={','.join(fanout_layers) or 'none'} rewired={rewired} opens={opens}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
