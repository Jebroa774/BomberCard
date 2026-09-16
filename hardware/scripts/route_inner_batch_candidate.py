"""Route a batch of short back-side control branches through In2.Cu.

This is a candidate-only pass.  It uses conservative back-side pad escapes,
0.30/0.10-mm B.Cu-to-In2 microvias and 0.20-mm signal tracks.  Failed
branches are reported and skipped so one dense endpoint does not discard the
rest of the batch; KiCad DRC is the final acceptance check.
"""

from __future__ import annotations

import argparse
from pathlib import Path
import shutil

import pcbnew

import route_lf_global as router
from restore_split_power_islands import microvia_obstacles


PAD_PAIRS = (
    ("pgood-u9-r109", "/CHARGER_PGOOD_N", "U9", "6", "R109", "1"),
    ("pgood-r109-u5", "/CHARGER_PGOOD_N", "R109", "1", "U5", "7"),
    ("chg-u9-r110", "/CHARGER_CHG_N", "U9", "5", "R110", "1"),
    ("chg-r110-u5", "/CHARGER_CHG_N", "R110", "1", "U5", "9"),
    ("bq-en2", "/BQ_EN2", "R128", "1", "U5", "5"),
    ("bq-en1-r115", "/BQ_EN1", "R115", "1", "U5", "6"),
    ("bq-en1-u18", "/BQ_EN1", "U5", "6", "U18", "4"),
    ("nfc-tx2-local", "/NFC_TX2", "U2", "6", "L302", "1"),
    ("nfc-vmid-local", "/NFC_VMID", "C301", "1", "U2", "9"),
    ("nfc-irq", "/NFC_IRQ_N", "U2", "25", "U1", "7"),
    ("strap-boot", "/STRAP_BOOT_TP", "TP202", "1", "U1", "16"),
    ("spi-miso-mcu", "/SPI_MISO", "R734", "1", "U1", "21"),
    ("subghz-cs", "/SUBGHZ_CS_N", "R404", "1", "U1", "22"),
    ("nfc-loadmod", "/NFC_LOADMOD", "U2", "2", "TP301", "1"),
    ("nfc-reset-r302", "/NFC_RESET_N", "U2", "38", "R302", "1"),
    ("pgood-u9-r109-front", "/CHARGER_PGOOD_N", "U9", "6", "R109", "1"),
    ("chg-u9-r110-front", "/CHARGER_CHG_N", "U9", "5", "R110", "1"),
    ("i2c-scl-u11-u10", "/I2C_SCL", "U11", "2", "U10", "13"),
    ("i2c-sda-u11-u10", "/I2C_SDA", "U11", "4", "U10", "14"),
    ("chg-ts-sj1-u5", "/CHG_TS", "SJ1", "1", "U5", "1"),
    ("cell-pos-u5-c120", "/CELL_POS", "U5", "3", "C120", "1"),
    ("esp-en-u1-j8", "/ESP_EN", "U1", "3", "J8", "10"),
    ("nfc-loop-a-r305-c310", "/NFC_LOOP_A", "R305", "1", "C310", "1"),
    ("nfc-vmid-r306-u2", "/NFC_VMID", "R306", "1", "U2", "9"),
    ("boot-u1-sw2", "/BOOT_N", "U1", "27", "SW2", "1"),
    ("boot-r204-sw2", "/BOOT_N", "R204", "1", "SW2", "1"),
    ("chg-disable-u5-r126", "/CHG_DISABLE", "U5", "4", "R126", "1"),
    ("chg-ts-fixed-r108-sj1", "/CHG_TS_FIXED", "R108", "1", "SJ1", "2"),
    ("sd-cs-u19-r510", "/SD_CS_DEV", "U19", "1", "R510", "2"),
    ("sd-cs-r510-r515", "/SD_CS_DEV", "R510", "2", "R515", "1"),
    ("fg-alert-u9-r125", "/FG_ALERT_N", "U9", "8", "R125", "1"),
)

# The accepted sensor-corridor route already reaches this exact In2.Cu point;
# only the U9 alert input branch is still open.
POINT_BRANCHES = (
    ("fg-alert-u9", "/FG_ALERT_N", "U9", "8", (79.6125, 37.5500)),
)

TRACK_WIDTH_MM = 0.20


def pad(board: pcbnew.BOARD, reference: str, number: str) -> pcbnew.PAD:
    footprint = board.FindFootprintByReference(reference)
    if footprint is None:
        raise RuntimeError(f"Missing footprint: {reference}")
    matches = [item for item in footprint.Pads() if item.GetNumber() == number]
    if not matches:
        raise RuntimeError(f"Expected {reference}.{number} pad, got none")
    if len(matches) > 1 and len({item.GetNetname() for item in matches}) != 1:
        raise RuntimeError(
            f"Ambiguous duplicate {reference}.{number} pads on different nets"
        )
    return matches[0]


def connected(board: pcbnew.BOARD, first: pcbnew.PAD, second: pcbnew.PAD) -> bool:
    board.BuildConnectivity()
    wanted = router.item_key(second)
    return any(
        router.item_key(item) == wanted
        for item in board.GetConnectivity().GetConnectedItems(first)
    )


def add_track(
    board: pcbnew.BOARD,
    net: pcbnew.NETINFO_ITEM,
    net_name: str,
    layer: int,
    start: tuple[float, float],
    end: tuple[float, float],
    obstacles: list[router.CopperObstacle],
) -> int:
    if router.distance(start, end) <= 0.001:
        return 0
    track = pcbnew.PCB_TRACK(board)
    track.SetStart(router.point(*start))
    track.SetEnd(router.point(*end))
    track.SetWidth(pcbnew.FromMM(TRACK_WIDTH_MM))
    track.SetLayer(layer)
    track.SetNet(net)
    track.SetLocked(True)
    board.Add(track)
    obstacles.append(
        router.CopperObstacle(
            net_name, "track", (start, end, TRACK_WIDTH_MM / 2.0, layer), track
        )
    )
    return 1


def add_microvia(
    board: pcbnew.BOARD,
    net: pcbnew.NETINFO_ITEM,
    net_name: str,
    position: tuple[float, float],
    obstacles: list[router.CopperObstacle],
) -> int:
    via = pcbnew.PCB_VIA(board)
    via.SetPosition(router.point(*position))
    via.SetWidth(pcbnew.FromMM(0.30))
    via.SetDrill(pcbnew.FromMM(0.10))
    via.SetViaType(pcbnew.VIATYPE_MICROVIA)
    via.SetLayerPair(pcbnew.In2_Cu, pcbnew.B_Cu)
    via.SetNet(net)
    via.SetLocked(True)
    board.Add(via)
    obstacles.append(router.CopperObstacle(net_name, "via", (position, 0.15), via))
    return 1


def add_through_via(
    board: pcbnew.BOARD,
    net: pcbnew.NETINFO_ITEM,
    net_name: str,
    position: tuple[float, float],
    obstacles: list[router.CopperObstacle],
) -> int:
    via = pcbnew.PCB_VIA(board)
    via.SetPosition(router.point(*position))
    via.SetWidth(pcbnew.FromMM(0.45))
    via.SetDrill(pcbnew.FromMM(0.20))
    via.SetViaType(pcbnew.VIATYPE_THROUGH)
    via.SetLayerPair(pcbnew.F_Cu, pcbnew.B_Cu)
    via.SetNet(net)
    via.SetLocked(True)
    board.Add(via)
    obstacles.append(router.CopperObstacle(net_name, "via", (position, 0.225), via))
    return 1


def add_front_microvia(
    board: pcbnew.BOARD,
    net: pcbnew.NETINFO_ITEM,
    net_name: str,
    position: tuple[float, float],
    obstacles: list[router.CopperObstacle],
) -> int:
    via = pcbnew.PCB_VIA(board)
    via.SetPosition(router.point(*position))
    via.SetWidth(pcbnew.FromMM(0.30))
    via.SetDrill(pcbnew.FromMM(0.10))
    via.SetViaType(pcbnew.VIATYPE_MICROVIA)
    via.SetLayerPair(pcbnew.F_Cu, pcbnew.In1_Cu)
    via.SetNet(net)
    via.SetLocked(True)
    board.Add(via)
    obstacles.append(router.CopperObstacle(net_name, "via", (position, 0.15), via))
    return 1


def add_buried_via(
    board: pcbnew.BOARD,
    net: pcbnew.NETINFO_ITEM,
    net_name: str,
    position: tuple[float, float],
    obstacles: list[router.CopperObstacle],
) -> int:
    via = pcbnew.PCB_VIA(board)
    via.SetPosition(router.point(*position))
    via.SetWidth(pcbnew.FromMM(0.45))
    via.SetDrill(pcbnew.FromMM(0.20))
    via.SetViaType(pcbnew.VIATYPE_BURIED)
    via.SetLayerPair(pcbnew.In1_Cu, pcbnew.In2_Cu)
    via.SetNet(net)
    via.SetLocked(True)
    board.Add(via)
    obstacles.append(router.CopperObstacle(net_name, "via", (position, 0.225), via))
    return 1


def front_stagger_candidates(
    *,
    net_name: str,
    pad_position: tuple[float, float],
    endpoint_ids: set[str],
    edge: router.Rect,
    obstacles: list[router.CopperObstacle],
) -> tuple[tuple[float, float], ...]:
    offsets: list[tuple[float, float]] = []
    for radius in (0.50, 0.75, 1.00, 1.25):
        offsets.extend(
            (
                (radius, 0.0),
                (-radius, 0.0),
                (0.0, radius),
                (0.0, -radius),
                (radius, radius),
                (radius, -radius),
                (-radius, radius),
                (-radius, -radius),
            )
        )
    relevant_via_obstacles = microvia_obstacles(
        obstacles, pcbnew.In1_Cu, pcbnew.In2_Cu
    )
    old_diameter = router.VIA_DIAMETER_MM
    old_drill = router.VIA_DRILL_MM
    router.VIA_DIAMETER_MM = 0.45
    router.VIA_DRILL_MM = 0.20
    result: list[tuple[float, float]] = []
    try:
        for dx, dy in offsets:
            candidate = (pad_position[0] + dx, pad_position[1] + dy)
            if not router.track_segment_is_clear(
                net_name=net_name,
                layer=pcbnew.In1_Cu,
                start=pad_position,
                end=candidate,
                width_mm=0.20,
                source_pads=endpoint_ids,
                edge=edge,
                obstacles=obstacles,
            ):
                continue
            if not router.signal_via_is_clear(
                net_name=net_name,
                position=candidate,
                endpoint_pad_ids=endpoint_ids,
                edge=edge,
                obstacles=relevant_via_obstacles,
            ):
                continue
            result.append(candidate)
    finally:
        router.VIA_DIAMETER_MM = old_diameter
        router.VIA_DRILL_MM = old_drill
    return tuple(result[:16])


def route_front_pair_on_in2(
    board: pcbnew.BOARD,
    net_name: str,
    start_pad: pcbnew.PAD,
    end_pad: pcbnew.PAD,
    edge: router.Rect,
    obstacles: list[router.CopperObstacle],
) -> tuple[int, int] | None:
    endpoint_ids = {router.item_key(start_pad), router.item_key(end_pad)}
    start_position = router.xy(start_pad.GetPosition())
    end_position = router.xy(end_pad.GetPosition())
    starts = front_stagger_candidates(
        net_name=net_name,
        pad_position=start_position,
        endpoint_ids=endpoint_ids,
        edge=edge,
        obstacles=obstacles,
    )
    ends = front_stagger_candidates(
        net_name=net_name,
        pad_position=end_position,
        endpoint_ids=endpoint_ids,
        edge=edge,
        obstacles=obstacles,
    )
    if not starts or not ends:
        return None
    for start_escape in starts:
        routed = router.find_fixed_layer_path_to_goals(
            net_name=net_name,
            start=start_escape,
            ends=ends,
            layer=pcbnew.In2_Cu,
            endpoint_pad_ids=endpoint_ids,
            edge=edge,
            obstacles=obstacles,
            expansion=12.0,
        )
        if routed is None:
            continue
        path, end_index = routed
        end_escape = ends[end_index]
        net = board.FindNet(net_name)
        assert net is not None
        vias = add_front_microvia(board, net, net_name, start_position, obstacles)
        vias += add_buried_via(board, net, net_name, start_escape, obstacles)
        vias += add_front_microvia(board, net, net_name, end_position, obstacles)
        vias += add_buried_via(board, net, net_name, end_escape, obstacles)
        tracks = add_track(
            board,
            net,
            net_name,
            pcbnew.In1_Cu,
            start_position,
            start_escape,
            obstacles,
        )
        tracks += add_track(
            board,
            net,
            net_name,
            pcbnew.In1_Cu,
            end_position,
            end_escape,
            obstacles,
        )
        tracks += sum(
            add_track(board, net, net_name, pcbnew.In2_Cu, start, end, obstacles)
            for start, end in zip(path, path[1:])
        )
        return tracks, vias
    return None


def back_stagger_candidates(
    *,
    net_name: str,
    pad_position: tuple[float, float],
    endpoint_ids: set[str],
    edge: router.Rect,
    obstacles: list[router.CopperObstacle],
) -> tuple[tuple[float, float], ...]:
    offsets: list[tuple[float, float]] = []
    for radius in (0.50, 0.75, 1.00, 1.25):
        offsets.extend(
            (
                (radius, 0.0),
                (-radius, 0.0),
                (0.0, radius),
                (0.0, -radius),
                (radius, radius),
                (radius, -radius),
                (-radius, radius),
                (-radius, -radius),
            )
        )
    relevant_via_obstacles = microvia_obstacles(
        obstacles, pcbnew.In2_Cu, pcbnew.In1_Cu
    )
    old_diameter = router.VIA_DIAMETER_MM
    old_drill = router.VIA_DRILL_MM
    router.VIA_DIAMETER_MM = 0.45
    router.VIA_DRILL_MM = 0.20
    result: list[tuple[float, float]] = []
    try:
        for dx, dy in offsets:
            candidate = (pad_position[0] + dx, pad_position[1] + dy)
            if not router.track_segment_is_clear(
                net_name=net_name,
                layer=pcbnew.In2_Cu,
                start=pad_position,
                end=candidate,
                width_mm=0.20,
                source_pads=endpoint_ids,
                edge=edge,
                obstacles=obstacles,
            ):
                continue
            if not router.signal_via_is_clear(
                net_name=net_name,
                position=candidate,
                endpoint_pad_ids=endpoint_ids,
                edge=edge,
                obstacles=relevant_via_obstacles,
            ):
                continue
            result.append(candidate)
    finally:
        router.VIA_DIAMETER_MM = old_diameter
        router.VIA_DRILL_MM = old_drill
    return tuple(result[:16])


def route_back_pair_on_in1(
    board: pcbnew.BOARD,
    net_name: str,
    start_pad: pcbnew.PAD,
    end_pad: pcbnew.PAD,
    edge: router.Rect,
    obstacles: list[router.CopperObstacle],
) -> tuple[int, int] | None:
    endpoint_ids = {router.item_key(start_pad), router.item_key(end_pad)}
    start_position = router.xy(start_pad.GetPosition())
    end_position = router.xy(end_pad.GetPosition())
    starts = back_stagger_candidates(
        net_name=net_name,
        pad_position=start_position,
        endpoint_ids=endpoint_ids,
        edge=edge,
        obstacles=obstacles,
    )
    ends = back_stagger_candidates(
        net_name=net_name,
        pad_position=end_position,
        endpoint_ids=endpoint_ids,
        edge=edge,
        obstacles=obstacles,
    )
    if not starts or not ends:
        return None
    for start_escape in starts:
        routed = router.find_fixed_layer_path_to_goals(
            net_name=net_name,
            start=start_escape,
            ends=ends,
            layer=pcbnew.In1_Cu,
            endpoint_pad_ids=endpoint_ids,
            edge=edge,
            obstacles=obstacles,
            expansion=12.0,
        )
        if routed is None:
            continue
        path, end_index = routed
        end_escape = ends[end_index]
        net = board.FindNet(net_name)
        assert net is not None
        vias = add_microvia(board, net, net_name, start_position, obstacles)
        vias += add_buried_via(board, net, net_name, start_escape, obstacles)
        vias += add_microvia(board, net, net_name, end_position, obstacles)
        vias += add_buried_via(board, net, net_name, end_escape, obstacles)
        tracks = add_track(
            board,
            net,
            net_name,
            pcbnew.In2_Cu,
            start_position,
            start_escape,
            obstacles,
        )
        tracks += add_track(
            board,
            net,
            net_name,
            pcbnew.In2_Cu,
            end_position,
            end_escape,
            obstacles,
        )
        tracks += sum(
            add_track(board, net, net_name, pcbnew.In1_Cu, start, end, obstacles)
            for start, end in zip(path, path[1:])
        )
        return tracks, vias
    return None


def add_pad_escape(
    board: pcbnew.BOARD,
    net: pcbnew.NETINFO_ITEM,
    net_name: str,
    escape: tuple[tuple[float, float], ...],
    obstacles: list[router.CopperObstacle],
    *,
    reverse: bool = False,
) -> tuple[int, int]:
    points = tuple(reversed(escape)) if reverse else escape
    tracks = sum(
        add_track(board, net, net_name, pcbnew.B_Cu, start, end, obstacles)
        for start, end in zip(points, points[1:])
    )
    return tracks, add_microvia(board, net, net_name, escape[-1], obstacles)


def route_pad_pair(
    board: pcbnew.BOARD,
    net_name: str,
    start_pad: pcbnew.PAD,
    end_pad: pcbnew.PAD,
    edge: router.Rect,
    obstacles: list[router.CopperObstacle],
    route_layer: int,
) -> tuple[int, int] | None:
    endpoint_ids = {router.item_key(start_pad), router.item_key(end_pad)}
    start_position = router.xy(start_pad.GetPosition())
    end_position = router.xy(end_pad.GetPosition())

    if (
        route_layer == pcbnew.In2_Cu
        and router.pad_layer(start_pad) == pcbnew.F_Cu
        and router.pad_layer(end_pad) == pcbnew.F_Cu
    ):
        staggered = route_front_pair_on_in2(
            board, net_name, start_pad, end_pad, edge, obstacles
        )
        if staggered is not None:
            return staggered

    if (
        route_layer == pcbnew.In1_Cu
        and router.pad_layer(start_pad) == pcbnew.B_Cu
        and router.pad_layer(end_pad) == pcbnew.B_Cu
    ):
        staggered = route_back_pair_on_in1(
            board, net_name, start_pad, end_pad, edge, obstacles
        )
        if staggered is not None:
            return staggered

    # The reviewed CHG_TMR route establishes this compact pattern for the
    # 0.5/0.65-mm-pitch back-side devices: a 0.30-mm microvia fits directly in
    # the land and avoids consuming the already crowded B.Cu fanout channel.
    direct = router.find_fixed_layer_path(
        net_name=net_name,
        start=start_position,
        end=end_position,
        layer=route_layer,
        endpoint_pad_ids=endpoint_ids,
        edge=edge,
        obstacles=obstacles,
        expansion=12.0,
    )
    if direct is not None:
        net = board.FindNet(net_name)
        assert net is not None
        vias = 0
        for endpoint_pad, position in (
            (start_pad, start_position),
            (end_pad, end_position),
        ):
            endpoint_layer = router.pad_layer(endpoint_pad)
            if endpoint_layer == route_layer:
                continue
            if route_layer == pcbnew.In2_Cu and endpoint_layer == pcbnew.B_Cu:
                vias += add_microvia(board, net, net_name, position, obstacles)
            elif route_layer == pcbnew.In1_Cu and endpoint_layer == pcbnew.F_Cu:
                vias += add_front_microvia(board, net, net_name, position, obstacles)
            elif route_layer in (pcbnew.F_Cu, pcbnew.B_Cu) and endpoint_layer in (
                pcbnew.F_Cu,
                pcbnew.B_Cu,
            ):
                vias += add_through_via(board, net, net_name, position, obstacles)
            else:
                return None
        tracks = sum(
            add_track(board, net, net_name, route_layer, start, end, obstacles)
            for start, end in zip(direct, direct[1:])
        )
        return tracks, vias

    if route_layer != pcbnew.In2_Cu:
        return None

    start_escapes = router.find_escape_paths(
        net_name=net_name,
        pad=start_pad,
        endpoint_pad_ids=endpoint_ids,
        edge=edge,
        obstacles=obstacles,
        maximum_paths=8,
    )
    end_escapes = router.find_escape_paths(
        net_name=net_name,
        pad=end_pad,
        endpoint_pad_ids=endpoint_ids,
        edge=edge,
        obstacles=obstacles,
        maximum_paths=8,
    )
    if not start_escapes or not end_escapes:
        return None
    end_positions = tuple(path[-1] for path in end_escapes)
    for start_escape in start_escapes:
        middle_result = router.find_fixed_layer_path_to_goals(
            net_name=net_name,
            start=start_escape[-1],
            ends=end_positions,
            layer=pcbnew.In2_Cu,
            endpoint_pad_ids=endpoint_ids,
            edge=edge,
            obstacles=obstacles,
            expansion=12.0,
        )
        if middle_result is None:
            continue
        middle, end_index = middle_result
        end_escape = end_escapes[end_index]
        net = board.FindNet(net_name)
        assert net is not None
        tracks, vias = add_pad_escape(
            board, net, net_name, start_escape, obstacles
        )
        added_tracks, added_vias = add_pad_escape(
            board, net, net_name, end_escape, obstacles, reverse=True
        )
        tracks += added_tracks
        vias += added_vias
        tracks += sum(
            add_track(board, net, net_name, pcbnew.In2_Cu, start, end, obstacles)
            for start, end in zip(middle, middle[1:])
        )
        return tracks, vias
    return None


def route_point_branch(
    board: pcbnew.BOARD,
    net_name: str,
    start_pad: pcbnew.PAD,
    target: tuple[float, float],
    edge: router.Rect,
    obstacles: list[router.CopperObstacle],
) -> tuple[int, int] | None:
    endpoint_ids = {router.item_key(start_pad)}
    start_position = router.xy(start_pad.GetPosition())
    direct = router.find_fixed_layer_path(
        net_name=net_name,
        start=start_position,
        end=target,
        layer=pcbnew.In2_Cu,
        endpoint_pad_ids=endpoint_ids,
        edge=edge,
        obstacles=obstacles,
        expansion=12.0,
    )
    if direct is not None:
        net = board.FindNet(net_name)
        assert net is not None
        vias = add_microvia(board, net, net_name, start_position, obstacles)
        tracks = sum(
            add_track(board, net, net_name, pcbnew.In2_Cu, start, end, obstacles)
            for start, end in zip(direct, direct[1:])
        )
        return tracks, vias

    escapes = router.find_escape_paths(
        net_name=net_name,
        pad=start_pad,
        endpoint_pad_ids=endpoint_ids,
        edge=edge,
        obstacles=obstacles,
        maximum_paths=10,
    )
    for escape in escapes:
        middle = router.find_fixed_layer_path(
            net_name=net_name,
            start=escape[-1],
            end=target,
            layer=pcbnew.In2_Cu,
            endpoint_pad_ids=endpoint_ids,
            edge=edge,
            obstacles=obstacles,
            expansion=12.0,
        )
        if middle is None:
            continue
        net = board.FindNet(net_name)
        assert net is not None
        tracks, vias = add_pad_escape(board, net, net_name, escape, obstacles)
        tracks += sum(
            add_track(board, net, net_name, pcbnew.In2_Cu, start, end, obstacles)
            for start, end in zip(middle, middle[1:])
        )
        return tracks, vias
    return None


def main() -> int:
    hardware_dir = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--select", action="append")
    parser.add_argument("--remove-track-uuid", action="append", default=[])
    parser.add_argument("--replace-existing", action="store_true")
    parser.add_argument(
        "--route-layer",
        choices=("F.Cu", "In1.Cu", "B.Cu", "In2.Cu"),
        default="In2.Cu",
    )
    parser.add_argument("--grid", type=float, default=0.25)
    parser.add_argument("--track-width", type=float, default=0.20)
    parser.add_argument("--clearance", type=float, default=0.20)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    global TRACK_WIDTH_MM
    TRACK_WIDTH_MM = args.track_width
    router.GRID_MM = args.grid
    router.TRACK_WIDTH_MM = args.track_width
    router.DIFFERENT_NET_CLEARANCE_MM = args.clearance
    input_path = args.input.resolve()
    output_path = args.output.resolve()
    if output_path == (hardware_dir / "PocketLab-Card.kicad_pcb").resolve():
        raise RuntimeError("Refusing to overwrite the authoritative PCB directly")
    if output_path.exists() and not args.force:
        raise RuntimeError(f"Output exists; use --force: {output_path}")

    board = pcbnew.LoadBoard(str(input_path))
    remove_ids = set(args.remove_track_uuid)
    try:
        edge = router.board_rect(board)
    except AttributeError:
        # KiCad 10's Windows SWIG wrapper can expose BOX2I as an opaque
        # SwigPyObject when helper modules were imported first.
        edge = router.Rect(20.20, 19.82, 105.80, 73.80)
    obstacles = router.existing_obstacles(board)
    for item in list(board.GetTracks()):
        item_uuid = (
            item.m_Uuid.AsString()
            if hasattr(item.m_Uuid, "AsString")
            else str(item.m_Uuid)
        )
        if item_uuid in remove_ids:
            board.Remove(item)
    selected = set(args.select or ())
    route_layer = {
        "F.Cu": pcbnew.F_Cu,
        "In1.Cu": pcbnew.In1_Cu,
        "B.Cu": pcbnew.B_Cu,
        "In2.Cu": pcbnew.In2_Cu,
    }[args.route_layer]
    total_tracks = 0
    total_vias = 0
    routed: list[str] = []
    failed: list[str] = []

    for label, net_name, first_ref, first_num, second_ref, second_num in PAD_PAIRS:
        if selected and label not in selected:
            continue
        first = pad(board, first_ref, first_num)
        second = pad(board, second_ref, second_num)
        if first.GetNetname() != net_name or second.GetNetname() != net_name:
            raise RuntimeError(f"Endpoint net mismatch: {label}")
        if connected(board, first, second) and not args.replace_existing:
            print(f"ALREADY {label}", flush=True)
            continue
        result = route_pad_pair(
            board, net_name, first, second, edge, obstacles, route_layer
        )
        if result is None:
            failed.append(label)
            print(f"FAILED {label}", flush=True)
            continue
        tracks, vias = result
        total_tracks += tracks
        total_vias += vias
        routed.append(label)
        print(f"ROUTED {label} tracks={tracks} vias={vias}", flush=True)

    for label, net_name, reference, number, target in POINT_BRANCHES:
        if selected and label not in selected:
            continue
        start = pad(board, reference, number)
        if start.GetNetname() != net_name:
            raise RuntimeError(f"Endpoint net mismatch: {label}")
        result = route_point_branch(board, net_name, start, target, edge, obstacles)
        if result is None:
            failed.append(label)
            print(f"FAILED {label}", flush=True)
            continue
        tracks, vias = result
        total_tracks += tracks
        total_vias += vias
        routed.append(label)
        print(f"ROUTED {label} tracks={tracks} vias={vias}", flush=True)

    pcbnew.ZONE_FILLER(board).Fill(board.Zones())
    output_path.parent.mkdir(parents=True, exist_ok=True)
    pcbnew.SaveBoard(str(output_path), board)
    for suffix in (".kicad_pro", ".kicad_dru"):
        shutil.copyfile(hardware_dir / f"PocketLab-Card{suffix}", output_path.with_suffix(suffix))

    reloaded = pcbnew.LoadBoard(str(output_path))
    reloaded.BuildConnectivity()
    connectivity = reloaded.GetConnectivity()
    connectivity.RecalculateRatsnest()
    print(
        f"SAVED routed={','.join(routed) or '-'} failed={','.join(failed) or '-'} "
        f"tracks={total_tracks} vias={total_vias} "
        f"opens={int(connectivity.GetUnconnectedCount(False))}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
