"""Create a candidate that reroutes CELL_NEG around the right SW5 locating hole."""

from __future__ import annotations

import argparse
from pathlib import Path
import shutil

import pcbnew


VIA_UUID = "4fd56153-b17e-4f34-a586-43f486c26d4a"
PWR_TRACK_UUID = "4735573b-375a-45a0-bc1f-684963a9e363"
REMOVE_TRACK_UUIDS = {
    "ef20e707-acf8-4814-b8f9-9a402646687e",
    "743229cd-998b-40fb-9526-ed8e5d52dc05",
    "6348b550-892f-47c3-bc96-a0915f0c398b",
    "55e38200-c266-4667-805d-0f61db73bc57",
}


def uuid_text(item: pcbnew.BOARD_ITEM) -> str:
    value = item.m_Uuid
    return value.AsString() if hasattr(value, "AsString") else str(value)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--via", default="95.55,68.90")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    if args.output.exists() and not args.force:
        raise RuntimeError(f"output exists: {args.output}")
    via_x, via_y = (float(value) for value in args.via.split(","))
    board = pcbnew.LoadBoard(str(args.input.resolve()))
    items = {uuid_text(item): item for item in board.GetTracks()}
    via = items[VIA_UUID]
    pwr_track = items[PWR_TRACK_UUID]
    if not isinstance(via, pcbnew.PCB_VIA):
        raise RuntimeError("CELL_NEG target is not a via")
    old_position = via.GetPosition()
    new_position = pcbnew.VECTOR2I_MM(via_x, via_y)
    via.SetPosition(new_position)
    if pwr_track.GetStart() == old_position:
        pwr_track.SetStart(new_position)
    elif pwr_track.GetEnd() == old_position:
        pwr_track.SetEnd(new_position)
    else:
        raise RuntimeError("PWR track is not attached to the CELL_NEG via")

    removed_width = None
    removed_layer = None
    net = via.GetNet()
    for target_uuid in REMOVE_TRACK_UUIDS:
        track = items[target_uuid]
        if isinstance(track, pcbnew.PCB_VIA):
            raise RuntimeError(f"unexpected via in removal set: {target_uuid}")
        removed_width = track.GetWidth()
        removed_layer = track.GetLayer()
        board.Remove(track)
    if removed_width is None or removed_layer is None:
        raise RuntimeError("no tracks removed")

    points = [
        (93.7875, 67.8000),
        (94.6500, 67.8000),
        (95.3500, 68.4000),
        (via_x, via_y),
        (95.6500, 69.4000),
        (96.1400, 70.0800),
    ]
    for start, end in zip(points, points[1:]):
        track = pcbnew.PCB_TRACK(board)
        track.SetStart(pcbnew.VECTOR2I_MM(*start))
        track.SetEnd(pcbnew.VECTOR2I_MM(*end))
        track.SetLayer(removed_layer)
        track.SetWidth(removed_width)
        track.SetNet(net)
        board.Add(track)

    pcbnew.ZONE_FILLER(board).Fill(board.Zones())
    board.BuildConnectivity()
    connectivity = board.GetConnectivity()
    connectivity.RecalculateRatsnest()
    opens = int(connectivity.GetUnconnectedCount(False))
    if opens:
        raise RuntimeError(f"reroute created {opens} open connection(s)")
    pcbnew.SaveBoard(str(args.output.resolve()), board)
    hardware_dir = Path(__file__).resolve().parent.parent
    for suffix in (".kicad_pro", ".kicad_dru"):
        shutil.copyfile(hardware_dir / f"PocketLab-Card{suffix}", args.output.with_suffix(suffix))
    print(f"REROUTED CELL_NEG via=({via_x:.3f},{via_y:.3f}) opens={opens}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
