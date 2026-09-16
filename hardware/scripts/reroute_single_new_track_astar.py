"""Replace one candidate track with a clearance-aware fixed-layer A* path.

The selected track is removed before obstacle collection so it cannot block
its own replacement.  The edit is always written to a separate candidate and
is accepted only when KiCad connectivity still reports zero open connections.
"""

from __future__ import annotations

import argparse
from pathlib import Path
import shutil

import pcbnew

import route_lf_global as maze
from route_plane_fanouts import board_rect, existing_obstacles, mm, point, xy


LAYER_BY_NAME = {
    "F.Cu": pcbnew.F_Cu,
    "In1.Cu": pcbnew.In1_Cu,
    "In2.Cu": pcbnew.In2_Cu,
    "B.Cu": pcbnew.B_Cu,
}


def uuid_text(item: pcbnew.BOARD_ITEM) -> str:
    value = item.m_Uuid
    return value.AsString() if hasattr(value, "AsString") else str(value)


def anchored_on_layer(
    board: pcbnew.BOARD,
    position: pcbnew.VECTOR2I,
    net_code: int,
    layer: int,
) -> bool:
    """Return true when existing same-net copper can enter ``layer`` here."""
    for item in board.GetTracks():
        if item.GetNetCode() != net_code:
            continue
        if not isinstance(item, pcbnew.PCB_VIA):
            if item.GetLayer() == layer and (
                item.GetStart() == position or item.GetEnd() == position
            ):
                return True
            continue
        if item.GetPosition() != position:
            continue
        if item.IsOnLayer(layer):
            return True
    for footprint in board.GetFootprints():
        for pad in footprint.Pads():
            if pad.GetNetCode() != net_code or pad.GetPosition() != position:
                continue
            if layer in set(pad.GetLayerSet().Seq()):
                return True
    return False


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--uuid", required=True)
    parser.add_argument("--layer", choices=tuple(LAYER_BY_NAME))
    parser.add_argument("--expansion", type=float, default=20.0)
    parser.add_argument(
        "--expected-opens",
        type=int,
        default=0,
        help="required ratsnest count after replacement",
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
    matches = [item for item in board.GetTracks() if uuid_text(item) == args.uuid]
    if len(matches) != 1:
        raise RuntimeError(f"expected one copper item UUID {args.uuid}, got {len(matches)}")
    original = matches[0]
    if isinstance(original, pcbnew.PCB_VIA):
        raise RuntimeError("selected UUID is a via")

    start_position = original.GetStart()
    end_position = original.GetEnd()
    start = xy(start_position)
    end = xy(end_position)
    net = original.GetNet()
    net_name = original.GetNetname()
    net_code = original.GetNetCode()
    width = original.GetWidth()
    width_mm = mm(width)
    locked = original.IsLocked()
    old_layer = original.GetLayer()
    new_layer = LAYER_BY_NAME[args.layer] if args.layer else old_layer

    board.Remove(original)
    if not anchored_on_layer(board, start_position, net_code, new_layer):
        raise RuntimeError(f"start is not anchored on {board.GetLayerName(new_layer)}")
    if not anchored_on_layer(board, end_position, net_code, new_layer):
        raise RuntimeError(f"end is not anchored on {board.GetLayerName(new_layer)}")

    maze.TRACK_WIDTH_MM = width_mm
    path = maze.find_fixed_layer_path(
        net_name=net_name,
        start=start,
        end=end,
        layer=new_layer,
        endpoint_pad_ids=set(),
        edge=board_rect(board),
        obstacles=existing_obstacles(board),
        expansion=args.expansion,
    )
    if path is None:
        raise RuntimeError(
            f"no fixed-layer path for {net_name} on {board.GetLayerName(new_layer)}"
        )

    added = 0
    for first, second in zip(path, path[1:]):
        if abs(first[0] - second[0]) < 1e-6 and abs(first[1] - second[1]) < 1e-6:
            continue
        segment = pcbnew.PCB_TRACK(board)
        segment.SetStart(point(*first))
        segment.SetEnd(point(*second))
        segment.SetLayer(new_layer)
        segment.SetWidth(width)
        segment.SetNet(net)
        segment.SetLocked(locked)
        board.Add(segment)
        added += 1

    pcbnew.ZONE_FILLER(board).Fill(board.Zones())
    board.BuildConnectivity()
    connectivity = board.GetConnectivity()
    connectivity.RecalculateRatsnest()
    opens = int(connectivity.GetUnconnectedCount(False))
    if opens != args.expected_opens:
        raise RuntimeError(
            f"replacement has {opens} open connection(s); "
            f"expected {args.expected_opens}"
        )

    pcbnew.SaveBoard(str(args.output.resolve()), board)
    for suffix in (".kicad_pro", ".kicad_dru"):
        shutil.copyfile(
            hardware_dir / f"PocketLab-Card{suffix}", args.output.with_suffix(suffix)
        )
    length = sum(
        ((second[0] - first[0]) ** 2 + (second[1] - first[1]) ** 2) ** 0.5
        for first, second in zip(path, path[1:])
    )
    print(
        f"REROUTED uuid={args.uuid} net={net_name} "
        f"{board.GetLayerName(old_layer)}->{board.GetLayerName(new_layer)} "
        f"segments={added} length={length:.3f}mm opens={opens}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
