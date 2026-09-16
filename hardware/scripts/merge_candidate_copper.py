"""Copy only new track/via geometry for selected nets into a PCB candidate."""

from __future__ import annotations

import argparse
from pathlib import Path
import shutil

import pcbnew


def key(item: pcbnew.BOARD_ITEM) -> tuple:
    if isinstance(item, pcbnew.PCB_VIA):
        return (
            "via",
            item.GetPosition().x,
            item.GetPosition().y,
            item.GetWidth(pcbnew.F_Cu),
            item.GetDrillValue(),
            int(item.GetViaType()),
            item.TopLayer(),
            item.BottomLayer(),
            item.GetNetname(),
        )
    assert isinstance(item, pcbnew.PCB_TRACK)
    first = (item.GetStart().x, item.GetStart().y)
    second = (item.GetEnd().x, item.GetEnd().y)
    if second < first:
        first, second = second, first
    return (
        "track",
        first,
        second,
        item.GetWidth(),
        item.GetLayer(),
        item.GetNetname(),
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", type=Path, required=True)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--net", action="append", required=True)
    parser.add_argument("--fill-zones", action="store_true")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    if args.output.resolve() == args.base.resolve():
        raise RuntimeError("output must differ from base")
    if args.output.exists() and not args.force:
        raise RuntimeError(f"output exists: {args.output}")

    base = pcbnew.LoadBoard(str(args.base.resolve()))
    source = pcbnew.LoadBoard(str(args.source.resolve()))
    selected = set(args.net)
    existing = {key(item) for item in base.GetTracks()}
    added_tracks = 0
    added_vias = 0
    for item in source.GetTracks():
        if item.GetNetname() not in selected or key(item) in existing:
            continue
        net = base.FindNet(item.GetNetname())
        if net is None:
            raise RuntimeError(f"missing base net: {item.GetNetname()}")
        if isinstance(item, pcbnew.PCB_VIA):
            copy = pcbnew.PCB_VIA(base)
            copy.SetPosition(item.GetPosition())
            # KiCad 10 requires a layer argument for PCB_VIA::GetWidth().
            copy.SetWidth(item.GetWidth(pcbnew.F_Cu))
            copy.SetDrill(item.GetDrillValue())
            copy.SetViaType(item.GetViaType())
            copy.SetLayerPair(item.TopLayer(), item.BottomLayer())
            added_vias += 1
        else:
            copy = pcbnew.PCB_TRACK(base)
            copy.SetStart(item.GetStart())
            copy.SetEnd(item.GetEnd())
            copy.SetWidth(item.GetWidth())
            copy.SetLayer(item.GetLayer())
            added_tracks += 1
        copy.SetNet(net)
        copy.SetLocked(item.IsLocked())
        base.Add(copy)
        existing.add(key(copy))

    if args.fill_zones:
        pcbnew.ZONE_FILLER(base).Fill(base.Zones())
    pcbnew.SaveBoard(str(args.output.resolve()), base)
    hardware_dir = Path(__file__).resolve().parent.parent
    for suffix in (".kicad_pro", ".kicad_dru"):
        shutil.copyfile(hardware_dir / f"PocketLab-Card{suffix}", args.output.with_suffix(suffix))
    print(f"MERGED tracks={added_tracks} vias={added_vias}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
