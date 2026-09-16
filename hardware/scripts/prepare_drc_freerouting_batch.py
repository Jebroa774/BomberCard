"""Build a Freerouting DSN whose pins are the actual KiCad DRC open endpoints."""

from __future__ import annotations

import argparse
from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal
import json
from pathlib import Path
import re

from prepare_selected_dsn import (
    normalize,
    obstacleize_unselected_wiring,
    quote_atom,
    select_network,
    split_network_classes,
)
from route_pcb import dsn_atom, dsn_expression_end


LAYER_ALIASES = {
    "F.Cu": "F.Cu",
    "B.Cu": "B.Cu",
    # KiCad's DSN exporter uses the user-visible layer names from this board,
    # not the canonical KiCad API names In1.Cu/In2.Cu.
    "GND": "GND",
    "PWR": "PWR",
}


@dataclass(frozen=True)
class Anchor:
    reference: str
    net: str
    layer: str
    x: str
    y: str


def dsn_number(value: float, invert: bool = False) -> str:
    number = Decimal(str(value)) * Decimal(1000)
    if invert:
        number = -number
    rendered = format(number, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return rendered or "0"


def item_layer(description: str) -> str:
    match = re.search(r"\bauf\s+(F\.Cu|B\.Cu|GND|PWR)\b", description)
    if match:
        return LAYER_ALIASES[match.group(1)]
    # Through-hole pads have no single-layer suffix; anchoring them on F.Cu is safe.
    if description.startswith("Durchsteckpad"):
        return "F.Cu"
    raise RuntimeError(f"Could not determine endpoint layer: {description}")


def selected_anchors(
    report: dict, selected: frozenset[str]
) -> tuple[list[Anchor], dict[str, list[str]], int]:
    anchors: list[Anchor] = []
    pins_by_net: dict[str, list[str]] = defaultdict(list)
    by_uuid: dict[str, Anchor] = {}
    edges = 0
    for entry in report.get("unconnected_items", []):
        items = entry.get("items", [])
        if len(items) != 2:
            continue
        match = re.search(r"\[([^\]]+)\]", items[0].get("description", ""))
        if not match:
            continue
        net = normalize(match.group(1))
        if net not in selected:
            continue
        edges += 1
        for item in items:
            item_uuid = item["uuid"]
            anchor = by_uuid.get(item_uuid)
            if anchor is None:
                position = item["pos"]
                reference = f"__DRC{len(by_uuid):04d}"
                anchor = Anchor(
                    reference=reference,
                    net=net,
                    layer=item_layer(item["description"]),
                    x=dsn_number(position["x"]),
                    y=dsn_number(position["y"], invert=True),
                )
                by_uuid[item_uuid] = anchor
                anchors.append(anchor)
            pin = f"{anchor.reference}-1"
            if pin not in pins_by_net[net]:
                pins_by_net[net].append(pin)
    missing = selected - pins_by_net.keys()
    if missing:
        raise RuntimeError(f"Selected nets have no DRC endpoints: {sorted(missing)}")
    return anchors, pins_by_net, edges


def replace_selected_net_pins(
    source: str, pins_by_net: dict[str, list[str]]
) -> str:
    matches = list(re.finditer(r"(?m)^\s*\(network\b", source))
    if len(matches) != 1:
        raise RuntimeError(f"Expected one network section, found {len(matches)}")
    network_start = source.find("(", matches[0].start())
    network_end = dsn_expression_end(source, network_start)
    head, cursor = dsn_atom(source, network_start + 1)
    if head != "network":
        raise RuntimeError(f"Expected network, got {head!r}")
    pieces = [source[:cursor]]
    copy_from = cursor
    while cursor < network_end - 1:
        while cursor < network_end - 1 and source[cursor].isspace():
            cursor += 1
        if cursor >= network_end - 1:
            break
        child_start = cursor
        child_end = dsn_expression_end(source, child_start)
        child_head, offset = dsn_atom(source, child_start + 1)
        child_name, _ = dsn_atom(source, offset)
        pieces.append(source[copy_from:child_start])
        if child_head == "net" and child_name in pins_by_net:
            indent_start = source.rfind("\n", 0, child_start) + 1
            indent = source[indent_start:child_start]
            pins = " ".join(quote_atom(pin) for pin in pins_by_net[child_name])
            pieces.append(
                f"(net {quote_atom(child_name)}\n"
                f"{indent}  (pins {pins})\n"
                f"{indent})"
            )
        else:
            pieces.append(source[child_start:child_end])
        copy_from = child_end
        cursor = child_end
    pieces.append(source[copy_from:])
    return "".join(pieces)


def insert_section_children(source: str, section: str, children: str) -> str:
    matches = list(re.finditer(rf"(?m)^\s*\({re.escape(section)}\b", source))
    if len(matches) != 1:
        raise RuntimeError(f"Expected one {section} section, found {len(matches)}")
    start = source.find("(", matches[0].start())
    end = dsn_expression_end(source, start)
    return source[: end - 1] + children + source[end - 1 :]


def strip_structure_planes(
    source: str, keep_nets: frozenset[str] = frozenset()
) -> tuple[str, int]:
    matches = list(re.finditer(r"(?m)^\s*\(structure\b", source))
    if len(matches) != 1:
        raise RuntimeError(f"Expected one structure section, found {len(matches)}")
    structure_start = source.find("(", matches[0].start())
    structure_end = dsn_expression_end(source, structure_start)
    head, cursor = dsn_atom(source, structure_start + 1)
    if head != "structure":
        raise RuntimeError(f"Expected structure, got {head!r}")
    pieces = [source[:cursor]]
    copy_from = cursor
    removed = 0
    while cursor < structure_end - 1:
        while cursor < structure_end - 1 and source[cursor].isspace():
            cursor += 1
        if cursor >= structure_end - 1:
            break
        child_start = cursor
        child_end = dsn_expression_end(source, child_start)
        child_head, _ = dsn_atom(source, child_start + 1)
        pieces.append(source[copy_from:child_start])
        if child_head == "plane":
            _, plane_cursor = dsn_atom(source, child_start + 1)
            plane_net, _ = dsn_atom(source, plane_cursor)
            if plane_net in keep_nets:
                pieces.append(source[child_start:child_end])
            else:
                removed += 1
        else:
            pieces.append(source[child_start:child_end])
        copy_from = child_end
        cursor = child_end
    pieces.append(source[copy_from:])
    return "".join(pieces), removed


def drop_selected_wiring(
    source: str, selected: frozenset[str]
) -> tuple[str, int]:
    """Remove obsolete fixed fragments for virtual-anchor nets.

    The real board keeps this copper.  In the temporary DSN the selected
    network pins have been replaced by DRC endpoint anchors, so retaining its
    old fragments only creates disconnected fixed items and false violations.
    """
    matches = list(re.finditer(r"(?m)^\s*\(wiring\b", source))
    if len(matches) != 1:
        raise RuntimeError(f"Expected one wiring section, found {len(matches)}")
    wiring_start = source.find("(", matches[0].start())
    wiring_end = dsn_expression_end(source, wiring_start)
    head, cursor = dsn_atom(source, wiring_start + 1)
    if head != "wiring":
        raise RuntimeError(f"Expected wiring, got {head!r}")
    pieces = [source[:cursor]]
    copy_from = cursor
    removed = 0
    while cursor < wiring_end - 1:
        while cursor < wiring_end - 1 and source[cursor].isspace():
            cursor += 1
        if cursor >= wiring_end - 1:
            break
        child_start = cursor
        child_end = dsn_expression_end(source, child_start)
        child_head, _ = dsn_atom(source, child_start + 1)
        pieces.append(source[copy_from:child_start])
        child = source[child_start:child_end]
        net_match = re.search(r"\(net\s+(\"(?:[^\"\\]|\\.)*\"|[^\s()]+)\s*\)", child)
        child_net = normalize(net_match.group(1)) if net_match else None
        if child_head in {"wire", "via"} and child_net in selected:
            removed += 1
        else:
            pieces.append(child)
        copy_from = child_end
        cursor = child_end
    pieces.append(source[copy_from:])
    return "".join(pieces), removed


def neutralize_unselected_wiring(
    source: str, selected: frozenset[str]
) -> tuple[str, int]:
    """Keep unselected copper fixed but remove its electrical net identity."""
    matches = list(re.finditer(r"(?m)^\s*\(wiring\b", source))
    if len(matches) != 1:
        raise RuntimeError(f"Expected one wiring section, found {len(matches)}")
    wiring_start = source.find("(", matches[0].start())
    wiring_end = dsn_expression_end(source, wiring_start)
    head, cursor = dsn_atom(source, wiring_start + 1)
    if head != "wiring":
        raise RuntimeError(f"Expected wiring, got {head!r}")
    pieces = [source[:cursor]]
    copy_from = cursor
    neutralized = 0
    while cursor < wiring_end - 1:
        while cursor < wiring_end - 1 and source[cursor].isspace():
            cursor += 1
        if cursor >= wiring_end - 1:
            break
        child_start = cursor
        child_end = dsn_expression_end(source, child_start)
        child = source[child_start:child_end]
        child_head, _ = dsn_atom(source, child_start + 1)
        pieces.append(source[copy_from:child_start])
        net_match = re.search(
            r"\(net\s+(\"(?:[^\"\\]|\\.)*\"|[^\s()]+)\s*\)", child
        )
        child_net = normalize(net_match.group(1)) if net_match else None
        if child_head in {"wire", "via"} and child_net not in selected and net_match:
            child = child[: net_match.start()] + child[net_match.end() :]
            neutralized += 1
        pieces.append(child)
        copy_from = child_end
        cursor = child_end
    pieces.append(source[copy_from:])
    return "".join(pieces), neutralized


def add_dummy_components(source: str, anchors: list[Anchor]) -> str:
    by_layer: dict[str, list[Anchor]] = defaultdict(list)
    for anchor in anchors:
        by_layer[anchor.layer].append(anchor)

    placement = ""
    library = ""
    for layer, layer_anchors in sorted(by_layer.items()):
        suffix = layer.replace(".", "_")
        image = f"__Codex_DRC_{suffix}"
        padstack = f"__Codex_DRC_PAD_{suffix}"
        placement += f"    (component {quote_atom(image)}\n"
        placement += "".join(
            f"      (place {anchor.reference} {anchor.x} {anchor.y} front 0)\n"
            for anchor in layer_anchors
        )
        placement += "    )\n"
        library += (
            f"    (image {quote_atom(image)}\n"
            f"      (pin {quote_atom(padstack)} 1 0 0)\n"
            f"    )\n"
            f"    (padstack {quote_atom(padstack)}\n"
            f"      (shape (circle {layer} 200))\n"
            f"      (attach off)\n"
            f"    )\n"
        )
    source = insert_section_children(source, "placement", placement)
    return insert_section_children(source, "library", library)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--drc", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--net", action="append", required=True)
    parser.add_argument("--strip-planes", action="store_true")
    parser.add_argument(
        "--signal-inner-layers",
        action="store_true",
        help="Mark GND/PWR as signal layers in the temporary DSN only",
    )
    parser.add_argument(
        "--split-classes",
        action="store_true",
        help="Keep all nets declared but put unselected nets in IGNORE_* classes",
    )
    parser.add_argument(
        "--keep-unselected-wiring",
        action="store_true",
        help="Keep existing unselected copper fixed instead of converting it to keepouts",
    )
    parser.add_argument(
        "--neutralize-unselected-wiring",
        action="store_true",
        help="Keep unselected copper fixed but remove its net identity",
    )
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    input_path = args.input.resolve()
    drc_path = args.drc.resolve()
    output_path = args.output.resolve()
    if not input_path.is_file() or not drc_path.is_file():
        raise RuntimeError("Input DSN or DRC report does not exist")
    if output_path.exists() and not args.force:
        raise RuntimeError(f"Output exists; use --force to replace it: {output_path}")

    selected = frozenset(normalize(net) for net in args.net)
    report = json.loads(drc_path.read_text(encoding="utf-8"))
    anchors, pins_by_net, edges = selected_anchors(report, selected)
    source = input_path.read_text(encoding="utf-8")
    if args.signal_inner_layers:
        source = re.sub(
            r"(\(layer\s+(?:GND|PWR)\s*\n\s*\(type\s+)power(\))",
            r"\1signal\2",
            source,
        )
    removed_planes = 0
    if args.strip_planes:
        source, removed_planes = strip_structure_planes(source)
    ignored_classes: tuple[str, ...] = ()
    if args.split_classes:
        result, ignored_classes = split_network_classes(source, selected)
    else:
        result = select_network(
            source,
            selected,
            stub_unselected=not args.neutralize_unselected_wiring,
        )
    result = replace_selected_net_pins(result, pins_by_net)
    obstacle_wires = 0
    obstacle_vias = 0
    neutralized_wiring = 0
    if args.neutralize_unselected_wiring:
        result, neutralized_wiring = neutralize_unselected_wiring(result, selected)
    elif not args.keep_unselected_wiring:
        result, obstacle_wires, obstacle_vias = obstacleize_unselected_wiring(
            result, selected
        )
    result, dropped_selected = drop_selected_wiring(result, selected)
    result = add_dummy_components(result, anchors)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(result, encoding="utf-8", newline="\n")
    print(
        f"Prepared DRC batch: nets={len(selected)}, edges={edges}, "
        f"anchors={len(anchors)}, obstacle_wires={obstacle_wires}, "
        f"obstacle_vias={obstacle_vias}, dropped_selected={dropped_selected}, "
        f"removed_planes={removed_planes}, "
        f"neutralized_wiring={neutralized_wiring}, "
        f"ignore_classes={','.join(ignored_classes)}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
