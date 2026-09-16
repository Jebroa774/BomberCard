"""Replace one reviewed KiCad segment with an explicit local polyline.

The edit is text-native so unchanged filled zones are preserved while route
alternatives are checked.  It refuses to overwrite the authoritative PCB and
requires the selected UUID to identify exactly one segment.
"""

from __future__ import annotations

import argparse
from pathlib import Path
import re
import shutil
import uuid

from restore_baseline_net import copper_blocks


LAYER_RE = re.compile(r'\(layer "([^"]+)"\)')
WIDTH_RE = re.compile(r"\(width\s+(-?\d+(?:\.\d+)?)\)")
NET_RE = re.compile(r'\(net "([^"]+)"\)')


def point(value: str) -> tuple[float, float]:
    parts = [part.strip() for part in value.split(",")]
    if len(parts) != 2:
        raise argparse.ArgumentTypeError("points must use x,y")
    return float(parts[0]), float(parts[1])


def segment(
    start: tuple[float, float],
    end: tuple[float, float],
    *,
    width: float,
    layer: str,
    net_name: str,
    item_uuid: str,
) -> str:
    return (
        "\t(segment\n"
        f"\t\t(start {start[0]:.6f} {start[1]:.6f})\n"
        f"\t\t(end {end[0]:.6f} {end[1]:.6f})\n"
        f"\t\t(width {width:.6f})\n"
        "\t\t(locked yes)\n"
        f"\t\t(layer \"{layer}\")\n"
        f"\t\t(net \"{net_name}\")\n"
        f"\t\t(uuid \"{item_uuid}\")\n"
        "\t)\n"
    )


def via(
    at: tuple[float, float],
    *,
    size: float,
    drill: float,
    via_kind: str,
    via_layers: tuple[str, str],
    net_name: str,
    item_uuid: str,
) -> str:
    kind_suffix = "" if via_kind == "through" else f" {via_kind}"
    return (
        f"\t(via{kind_suffix}\n"
        f"\t\t(at {at[0]:.6f} {at[1]:.6f})\n"
        f"\t\t(size {size:.6f})\n"
        f"\t\t(drill {drill:.6f})\n"
        f"\t\t(layers \"{via_layers[0]}\" \"{via_layers[1]}\")\n"
        "\t\t(locked yes)\n"
        f"\t\t(net \"{net_name}\")\n"
        f"\t\t(uuid \"{item_uuid}\")\n"
        "\t)\n"
    )


def main() -> int:
    hardware_dir = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--uuid", required=True)
    parser.add_argument("--point", type=point, action="append", required=True)
    parser.add_argument("--layer")
    parser.add_argument("--via", type=point, action="append", default=[])
    parser.add_argument("--via-size", type=float, default=0.5)
    parser.add_argument("--via-drill", type=float, default=0.3)
    parser.add_argument("--via-kind", choices=("through", "micro", "blind"), default="through")
    parser.add_argument("--via-layers", nargs=2, default=("F.Cu", "B.Cu"))
    parser.add_argument("--remove-uuid", action="append", default=[])
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    if len(args.point) < 2:
        raise RuntimeError("At least two --point arguments are required")

    output_path = args.output.resolve()
    if output_path == (hardware_dir / "PocketLab-Card.kicad_pcb").resolve():
        raise RuntimeError("Refusing to overwrite the authoritative PCB")
    if output_path.exists() and not args.force:
        raise RuntimeError(f"Output exists; use --force: {output_path}")

    input_path = args.input.resolve()
    text = input_path.read_text(encoding="utf-8")
    selected = [block for block in copper_blocks(text) if block[3] == args.uuid]
    if len(selected) != 1:
        raise RuntimeError(f"Expected one segment UUID {args.uuid}; found {len(selected)}")
    start, end, kind, _uuid, _net = selected[0]
    if kind != "segment":
        raise RuntimeError(f"UUID is not a segment: {args.uuid}")
    old_block = text[start:end]
    layer_match = LAYER_RE.search(old_block)
    width_match = WIDTH_RE.search(old_block)
    net_match = NET_RE.search(old_block)
    if not layer_match or not width_match or not net_match:
        raise RuntimeError("Selected segment is missing layer, width or net")
    selected_layer = args.layer or layer_match.group(1)
    replacement = "".join(
        segment(
            first,
            second,
            width=float(width_match.group(1)),
            layer=selected_layer,
            net_name=net_match.group(1),
            item_uuid=args.uuid if index == 0 else str(uuid.uuid4()),
        )
        for index, (first, second) in enumerate(zip(args.point, args.point[1:]))
    )
    replacement += "".join(
        via(
            at,
            size=args.via_size,
            drill=args.via_drill,
            via_kind=args.via_kind,
            via_layers=tuple(args.via_layers),
            net_name=net_match.group(1),
            item_uuid=str(uuid.uuid4()),
        )
        for at in args.via
    )
    if args.uuid in args.remove_uuid:
        raise RuntimeError("The replaced segment UUID cannot also be removed")
    edits = [(start, end, replacement)]
    all_blocks = copper_blocks(text)
    for remove_uuid in args.remove_uuid:
        matches = [block for block in all_blocks if block[3] == remove_uuid]
        if len(matches) != 1:
            raise RuntimeError(
                f"Expected one copper item UUID {remove_uuid}; found {len(matches)}"
            )
        remove_start, remove_end, _kind, _uuid, _net = matches[0]
        edits.append((remove_start, remove_end, ""))
    result = text
    for edit_start, edit_end, edit_text in sorted(edits, reverse=True):
        result = result[:edit_start] + edit_text + result[edit_end:]
    output_path.write_text(result, encoding="utf-8", newline="\n")
    for suffix in (".kicad_pro", ".kicad_dru"):
        shutil.copyfile(hardware_dir / f"PocketLab-Card{suffix}", output_path.with_suffix(suffix))
    print(
        f"Replaced {args.uuid} on {selected_layer} "
        f"with {len(args.point) - 1} segment(s) and {len(args.via)} via(s); "
        f"removed {len(args.remove_uuid)} copper item(s)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
