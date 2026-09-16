"""Merge only newly routed Specctra session copper into an existing KiCad board.

Unlike KiCad's full SES importer, this keeps every pre-existing track and via.  It
is intended for selected-net Freerouting batches whose unselected copper was
represented by router-only keepouts.
"""

from __future__ import annotations

import argparse
from collections import defaultdict
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
import re
import shutil

import pcbnew

from route_pcb import dsn_atom, dsn_expression_end
from normalize_specctra_wiring import VIA_RE, WIRE_RE


LAYER_INDEX = {
    0: pcbnew.F_Cu,
    1: pcbnew.In1_Cu,
    2: pcbnew.In2_Cu,
    3: pcbnew.B_Cu,
}
VIA_NAME_RE = re.compile(r"^Via\[(\d+)-(\d+)\]_(\d+):(\d+)_um$")


def resolution_nm_per_unit(session: str) -> Decimal:
    match = re.search(r"\(resolution\s+(um|mm|mil)\s+([0-9.]+)\)", session)
    if not match:
        raise RuntimeError("Session has no supported resolution")
    nanometres = {
        "um": Decimal(1000),
        "mm": Decimal(1_000_000),
        "mil": Decimal(25_400),
    }[match.group(1)]
    return nanometres / Decimal(match.group(2))


def to_nm(atom: str, scale: Decimal, invert: bool = False) -> int:
    value = Decimal(atom) * scale
    if invert:
        value = -value
    return int(value.to_integral_value(rounding=ROUND_HALF_UP))


def unquote_atom(atom: str) -> str:
    value, offset = dsn_atom(atom, 0)
    if atom[offset:].strip():
        raise RuntimeError(f"Unexpected trailing text in atom: {atom!r}")
    return value


def track_key(
    net_name: str,
    layer_name: str,
    width: int,
    start: pcbnew.VECTOR2I,
    end: pcbnew.VECTOR2I,
) -> tuple[str, str, int, tuple[int, int], tuple[int, int]]:
    first = (start.x, start.y)
    second = (end.x, end.y)
    if second < first:
        first, second = second, first
    return net_name, layer_name, width, first, second


def existing_dsn_items(
    source: str,
) -> tuple[
    set[tuple[str, str, int, tuple[int, int], tuple[int, int]]],
    set[tuple[str, str, int, int]],
]:
    """Return physical segment/via keys already present in an input DSN."""
    # KiCad's DSN wiring coordinates are emitted in micrometres even though
    # the matching SES network_out coordinates use the declared resolution.
    # For KiCad's `(resolution um 10)` export this is a factor of ten.
    scale = resolution_nm_per_unit(source) * Decimal(10)
    tracks: set[tuple[str, str, int, tuple[int, int], tuple[int, int]]] = set()
    vias: set[tuple[str, str, int, int]] = set()
    for line in source.splitlines():
        wire = WIRE_RE.match(line)
        if wire:
            tokens = wire.group("coords").split()
            points = [
                pcbnew.VECTOR2I(
                    to_nm(x, scale), to_nm(y, scale, invert=True)
                )
                for x, y in zip(tokens[0::2], tokens[1::2], strict=True)
            ]
            net_name = unquote_atom(wire.group("net"))
            layer_name = unquote_atom(wire.group("layer"))
            width = to_nm(wire.group("width"), scale)
            for start, end in zip(points, points[1:]):
                tracks.add(track_key(net_name, layer_name, width, start, end))
            continue
        via = VIA_RE.match(line)
        if via:
            vias.add(
                (
                    unquote_atom(via.group("net")),
                    unquote_atom(via.group("padstack")),
                    to_nm(via.group("x"), scale),
                    to_nm(via.group("y"), scale, invert=True),
                )
            )
    return tracks, vias


def expression_children(text: str, start: int, end: int):
    cursor = start
    while cursor < end - 1:
        while cursor < end - 1 and text[cursor].isspace():
            cursor += 1
        if cursor >= end - 1:
            return
        child_end = dsn_expression_end(text, cursor)
        yield cursor, child_end
        cursor = child_end


def path_data(child: str) -> tuple[str, str, list[tuple[str, str]]]:
    path_start = child.find("(path")
    if path_start < 0:
        raise RuntimeError("Session wire has no path")
    path_end = dsn_expression_end(child, path_start)
    head, offset = dsn_atom(child, path_start + 1)
    if head != "path":
        raise RuntimeError(f"Expected path, got {head!r}")
    layer, offset = dsn_atom(child, offset)
    width, offset = dsn_atom(child, offset)
    coordinates: list[str] = []
    while offset < path_end - 1:
        while offset < path_end - 1 and child[offset].isspace():
            offset += 1
        if offset >= path_end - 1:
            break
        atom, offset = dsn_atom(child, offset)
        coordinates.append(atom)
    if len(coordinates) < 4 or len(coordinates) % 2:
        raise RuntimeError("Session path has invalid coordinates")
    return layer, width, list(
        zip(coordinates[0::2], coordinates[1::2], strict=True)
    )


def session_network_items(session: str):
    matches = list(re.finditer(r"(?m)^\s*\(network_out\b", session))
    if len(matches) != 1:
        raise RuntimeError(f"Expected one network_out section, found {len(matches)}")
    network_start = session.find("(", matches[0].start())
    network_end = dsn_expression_end(session, network_start)
    head, network_offset = dsn_atom(session, network_start + 1)
    if head != "network_out":
        raise RuntimeError(f"Expected network_out, got {head!r}")
    for net_start, net_end in expression_children(
        session, network_offset, network_end
    ):
        head, offset = dsn_atom(session, net_start + 1)
        if head != "net":
            continue
        net_name, offset = dsn_atom(session, offset)
        for item_start, item_end in expression_children(session, offset, net_end):
            item_head, item_offset = dsn_atom(session, item_start + 1)
            item = session[item_start:item_end]
            if item_head == "wire":
                yield net_name, "wire", path_data(item)
            elif item_head == "via":
                padstack, item_offset = dsn_atom(session, item_offset)
                x, item_offset = dsn_atom(session, item_offset)
                y, _ = dsn_atom(session, item_offset)
                yield net_name, "via", (padstack, x, y)


def unconnected_count(board: pcbnew.BOARD) -> int:
    board.BuildConnectivity()
    connectivity = board.GetConnectivity()
    connectivity.RecalculateRatsnest()
    return int(connectivity.GetUnconnectedCount(False))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", type=Path, required=True)
    parser.add_argument("--session", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--net",
        action="append",
        help="Merge only the named net; may be repeated",
    )
    parser.add_argument(
        "--input-dsn",
        type=Path,
        help="Skip copper already present in this Freerouting input DSN",
    )
    args = parser.parse_args()

    base = args.base.resolve()
    session_path = args.session.resolve()
    output = args.output.resolve()
    if output == base:
        raise RuntimeError("Output must be separate from base board")
    if not base.is_file() or not session_path.is_file():
        raise RuntimeError("Base board or session does not exist")

    session = session_path.read_text(encoding="utf-8")
    scale = resolution_nm_per_unit(session)
    existing_tracks = set()
    existing_vias = set()
    if args.input_dsn is not None:
        input_dsn = args.input_dsn.resolve()
        if not input_dsn.is_file():
            raise RuntimeError(f"Input DSN does not exist: {input_dsn}")
        existing_tracks, existing_vias = existing_dsn_items(
            input_dsn.read_text(encoding="utf-8")
        )
    board = pcbnew.LoadBoard(str(base))
    selected_nets = set(args.net or [])
    opens_before = unconnected_count(board)
    added_tracks = 0
    added_vias = 0
    skipped_tracks = 0
    skipped_vias = 0
    new_by_net: dict[str, list[int]] = defaultdict(lambda: [0, 0])

    for net_name, item_type, data in session_network_items(session):
        if selected_nets and net_name not in selected_nets:
            continue
        net = board.FindNet(net_name)
        if net is None:
            raise RuntimeError(f"Session net is absent from board: {net_name}")
        if item_type == "wire":
            layer_name, width_atom, points = data
            layer = board.GetLayerID(layer_name)
            if layer < 0:
                raise RuntimeError(f"Session layer is absent from board: {layer_name}")
            width = to_nm(width_atom, scale)
            positions = [
                pcbnew.VECTOR2I(to_nm(x, scale), to_nm(y, scale, invert=True))
                for x, y in points
            ]
            for start, end in zip(positions, positions[1:]):
                if start == end:
                    continue
                if track_key(net_name, layer_name, width, start, end) in existing_tracks:
                    skipped_tracks += 1
                    continue
                track = pcbnew.PCB_TRACK(board)
                track.SetStart(start)
                track.SetEnd(end)
                track.SetWidth(width)
                track.SetLayer(layer)
                track.SetNet(net)
                board.Add(track)
                added_tracks += 1
                new_by_net[net_name][0] += 1
        elif item_type == "via":
            padstack, x, y = data
            match = VIA_NAME_RE.match(padstack)
            if not match:
                raise RuntimeError(f"Unsupported session via: {padstack}")
            start_index, end_index, diameter_um, drill_um = map(
                int, match.groups()
            )
            via_x = to_nm(x, scale)
            via_y = to_nm(y, scale, invert=True)
            if (net_name, padstack, via_x, via_y) in existing_vias:
                skipped_vias += 1
                continue
            if start_index not in LAYER_INDEX or end_index not in LAYER_INDEX:
                raise RuntimeError(f"Unsupported via layer span: {padstack}")
            via = pcbnew.PCB_VIA(board)
            via.SetPosition(
                pcbnew.VECTOR2I(via_x, via_y)
            )
            via.SetWidth(diameter_um * 1000)
            via.SetDrill(drill_um * 1000)
            start_layer = LAYER_INDEX[start_index]
            end_layer = LAYER_INDEX[end_index]
            via.SetLayerPair(start_layer, end_layer)
            if start_layer == pcbnew.F_Cu and end_layer == pcbnew.B_Cu:
                via.SetViaType(pcbnew.VIATYPE_THROUGH)
            elif abs(start_index - end_index) == 1:
                via.SetViaType(pcbnew.VIATYPE_MICROVIA)
            else:
                via.SetViaType(pcbnew.VIATYPE_BLIND)
            via.SetNet(net)
            board.Add(via)
            added_vias += 1
            new_by_net[net_name][1] += 1

    opens_after = unconnected_count(board)
    pcbnew.SaveBoard(str(output), board)
    hardware_dir = Path(__file__).resolve().parent.parent
    for suffix in (".kicad_pro", ".kicad_dru"):
        shutil.copyfile(
            hardware_dir / f"PocketLab-Card{suffix}",
            output.with_suffix(suffix),
        )
    reloaded = pcbnew.LoadBoard(str(output))
    reload_opens = unconnected_count(reloaded)
    print(
        f"Merged session: tracks={added_tracks}, vias={added_vias}, "
        f"skipped_existing_tracks={skipped_tracks}, "
        f"skipped_existing_vias={skipped_vias}, "
        f"opens={opens_before}->{opens_after}, reload_opens={reload_opens}"
    )
    for net_name, (tracks, vias) in sorted(new_by_net.items()):
        print(f"NEW_NET {net_name} tracks={tracks} vias={vias}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
