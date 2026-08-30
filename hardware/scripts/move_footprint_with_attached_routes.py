"""Move one footprint and every route endpoint/via attached to its pads.

This writes a separate candidate board and refuses to overwrite the input or the
authoritative project board.  It is intended for small placement corrections
where the existing fan-outs should move rigidly with the component pads.
"""

from __future__ import annotations

import argparse
from pathlib import Path
import shutil

import pcbnew


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--reference", required=True)
    parser.add_argument("--x", type=float, required=True)
    parser.add_argument("--y", type=float, required=True)
    parser.add_argument("--skip-fill-zones", action="store_true")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    hardware_dir = Path(__file__).resolve().parent.parent
    authoritative = (hardware_dir / "PocketLab-Card.kicad_pcb").resolve()
    input_path = args.input.resolve()
    output_path = args.output.resolve()
    if output_path in {authoritative, input_path}:
        raise RuntimeError("output must be a separate non-authoritative board")
    if output_path.exists() and not args.force:
        raise RuntimeError(f"output exists: {output_path}")

    board = pcbnew.LoadBoard(str(input_path))
    footprint = board.FindFootprintByReference(args.reference)
    if footprint is None:
        raise RuntimeError(f"missing footprint: {args.reference}")

    old_footprint_position = footprint.GetPosition()
    new_footprint_position = pcbnew.VECTOR2I_MM(args.x, args.y)
    delta = new_footprint_position - old_footprint_position
    pad_moves = [
        (pad.GetNetCode(), pad.GetPosition(), pad.GetPosition() + delta)
        for pad in footprint.Pads()
        if pad.GetNetCode()
    ]

    moved_vias: set[str] = set()
    moved_track_ends = 0
    for net_code, old_pad_position, new_pad_position in pad_moves:
        for item in board.GetTracks():
            if item.GetNetCode() != net_code:
                continue
            if isinstance(item, pcbnew.PCB_VIA):
                if item.GetPosition() == old_pad_position:
                    item.SetPosition(new_pad_position)
                    moved_vias.add(item.m_Uuid.AsString())
                continue
            if item.GetStart() == old_pad_position:
                item.SetStart(new_pad_position)
                moved_track_ends += 1
            if item.GetEnd() == old_pad_position:
                item.SetEnd(new_pad_position)
                moved_track_ends += 1

    footprint.SetPosition(new_footprint_position)
    if not args.skip_fill_zones:
        pcbnew.ZONE_FILLER(board).Fill(board.Zones())

    board.BuildConnectivity()
    connectivity = board.GetConnectivity()
    connectivity.RecalculateRatsnest()
    opens = int(connectivity.GetUnconnectedCount(False))
    if opens:
        raise RuntimeError(f"footprint move created {opens} open connection(s)")

    pcbnew.SaveBoard(str(output_path), board)
    for suffix in (".kicad_pro", ".kicad_dru"):
        shutil.copyfile(
            hardware_dir / f"PocketLab-Card{suffix}", output_path.with_suffix(suffix)
        )
    print(
        f"MOVED reference={args.reference} to=({args.x:.4f},{args.y:.4f}) "
        f"vias={len(moved_vias)} track_ends={moved_track_ends} opens={opens}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
