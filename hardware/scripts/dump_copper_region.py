"""Print copper objects intersecting a rectangular board region."""

from __future__ import annotations

import argparse
from pathlib import Path

import pcbnew


def uuid_text(item: pcbnew.BOARD_ITEM) -> str:
    value = item.m_Uuid
    return value.AsString() if hasattr(value, "AsString") else str(value)


def mm(value: int) -> float:
    return pcbnew.ToMM(value)


def point_mm(point: pcbnew.VECTOR2I) -> tuple[float, float]:
    return mm(point.x), mm(point.y)


def intersects(box: pcbnew.BOX2I, query: pcbnew.BOX2I) -> bool:
    return box.Intersects(query)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("board", type=Path)
    parser.add_argument("--box", required=True, help="xmin,ymin,xmax,ymax in mm")
    parser.add_argument(
        "--layers",
        default="F.Cu,In2.Cu,B.Cu",
        help="Comma-separated copper layers",
    )
    args = parser.parse_args()

    xmin, ymin, xmax, ymax = (float(value) for value in args.box.split(","))
    query = pcbnew.BOX2I(
        pcbnew.VECTOR2I_MM(xmin, ymin),
        pcbnew.VECTOR2I_MM(xmax - xmin, ymax - ymin),
    )
    board = pcbnew.LoadBoard(str(args.board.resolve()))
    layer_ids = {
        "F.Cu": pcbnew.F_Cu,
        "In1.Cu": pcbnew.In1_Cu,
        "In2.Cu": pcbnew.In2_Cu,
        "B.Cu": pcbnew.B_Cu,
    }
    selected = {layer_ids[name] for name in args.layers.split(",")}

    print("PADS")
    for footprint in board.GetFootprints():
        reference = footprint.GetReference()
        for pad in footprint.Pads():
            layers = {layer for layer in selected if pad.IsOnLayer(layer)}
            if layers and intersects(pad.GetBoundingBox(), query):
                pos = point_mm(pad.GetPosition())
                size = point_mm(pad.GetSize())
                layer_names = ",".join(board.GetLayerName(layer) for layer in sorted(layers))
                print(
                    f"PAD {reference}.{pad.GetNumber()} net={pad.GetNetname()} "
                    f"pos={pos[0]:.4f},{pos[1]:.4f} size={size[0]:.4f},{size[1]:.4f} "
                    f"layers={layer_names}"
                )

    print("TRACKS_AND_VIAS")
    for item in board.GetTracks():
        if not intersects(item.GetBoundingBox(), query):
            continue
        if isinstance(item, pcbnew.PCB_VIA):
            pos = point_mm(item.GetPosition())
            print(
                f"VIA uuid={uuid_text(item)} net={item.GetNetname()} "
                f"pos={pos[0]:.4f},{pos[1]:.4f} "
                f"diam={mm(item.GetWidth(pcbnew.F_Cu)):.4f} drill={mm(item.GetDrillValue()):.4f}"
            )
        elif item.GetLayer() in selected:
            start = point_mm(item.GetStart())
            end = point_mm(item.GetEnd())
            print(
                f"TRACK uuid={uuid_text(item)} net={item.GetNetname()} "
                f"layer={board.GetLayerName(item.GetLayer())} "
                f"start={start[0]:.4f},{start[1]:.4f} end={end[0]:.4f},{end[1]:.4f} "
                f"width={mm(item.GetWidth()):.4f}"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
