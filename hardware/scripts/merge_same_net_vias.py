"""Merge reviewed same-net via pairs while preserving attached fanouts.

Every track endpoint attached to the removed via is moved to the kept via.
Tracks that collapse to zero length are removed.  The output is rejected if
KiCad reports any open connection after the edit.
"""

from __future__ import annotations

import argparse
from pathlib import Path
import shutil

import pcbnew


def uuid_text(item: pcbnew.BOARD_ITEM) -> str:
    value = item.m_Uuid
    return value.AsString() if hasattr(value, "AsString") else str(value)


def pair(value: str) -> tuple[str, str]:
    pieces = [piece.strip() for piece in value.split(",", 1)]
    if len(pieces) != 2 or not all(pieces):
        raise argparse.ArgumentTypeError("pairs must use REMOVE_UUID,KEEP_UUID")
    return pieces[0], pieces[1]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--pair", action="append", type=pair, required=True)
    parser.add_argument(
        "--make-kept-through",
        action="store_true",
        help="replace each kept stacked via with one F.Cu-B.Cu through via",
    )
    parser.add_argument("--through-diameter", type=float, default=0.45)
    parser.add_argument("--through-drill", type=float, default=0.20)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    hardware_dir = Path(__file__).resolve().parent.parent
    authoritative = (hardware_dir / "PocketLab-Card.kicad_pcb").resolve()
    if args.output.resolve() in {authoritative, args.input.resolve()}:
        raise RuntimeError("output must be a separate non-authoritative board")
    if args.output.exists() and not args.force:
        raise RuntimeError(f"output exists: {args.output}")

    board = pcbnew.LoadBoard(str(args.input.resolve()))
    moved_endpoints = 0
    collapsed_tracks = 0
    all_tracks = list(board.GetTracks())
    by_uuid = {uuid_text(item): item for item in all_tracks}
    removed_items: set[str] = set()
    kept_items: set[str] = set()
    for remove_uuid, keep_uuid in args.pair:
        removed = by_uuid.get(remove_uuid)
        kept = by_uuid.get(keep_uuid)
        if not isinstance(removed, pcbnew.PCB_VIA):
            raise RuntimeError(f"remove UUID is not a via: {remove_uuid}")
        if not isinstance(kept, pcbnew.PCB_VIA):
            raise RuntimeError(f"keep UUID is not a via: {keep_uuid}")
        if removed.GetNetCode() != kept.GetNetCode():
            raise RuntimeError(f"vias are on different nets: {remove_uuid}, {keep_uuid}")

        old = removed.GetPosition()
        new = kept.GetPosition()
        for track in all_tracks:
            if uuid_text(track) in removed_items:
                continue
            if isinstance(track, pcbnew.PCB_VIA) or track.GetNetCode() != removed.GetNetCode():
                continue
            changed = False
            if track.GetStart() == old:
                track.SetStart(new)
                changed = True
            if track.GetEnd() == old:
                track.SetEnd(new)
                changed = True
            if not changed:
                continue
            moved_endpoints += 1
            if track.GetStart() == track.GetEnd():
                board.Remove(track)
                removed_items.add(uuid_text(track))
                collapsed_tracks += 1
        board.Remove(removed)
        removed_items.add(remove_uuid)
        kept_items.add(keep_uuid)
        print(
            f"MERGED net={kept.GetNetname()} remove={remove_uuid} keep={keep_uuid}",
            flush=True,
        )

    if args.make_kept_through:
        for keep_uuid in kept_items:
            kept = by_uuid[keep_uuid]
            if not isinstance(kept, pcbnew.PCB_VIA):
                raise RuntimeError(f"kept UUID is not a via: {keep_uuid}")
            kept.SetViaType(pcbnew.VIATYPE_THROUGH)
            kept.SetLayerPair(pcbnew.F_Cu, pcbnew.B_Cu)
            kept.SetWidth(pcbnew.FromMM(args.through_diameter))
            kept.SetDrill(pcbnew.FromMM(args.through_drill))

    board.BuildConnectivity()
    connectivity = board.GetConnectivity()
    connectivity.RecalculateRatsnest()
    opens = int(connectivity.GetUnconnectedCount(False))
    if opens:
        raise RuntimeError(f"via merge created {opens} open connection(s)")

    pcbnew.SaveBoard(str(args.output.resolve()), board)
    for suffix in (".kicad_pro", ".kicad_dru"):
        shutil.copyfile(
            hardware_dir / f"PocketLab-Card{suffix}", args.output.with_suffix(suffix)
        )
    print(
        f"SAVED pairs={len(args.pair)} moved_endpoints={moved_endpoints} "
        f"collapsed_tracks={collapsed_tracks} opens={opens}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
