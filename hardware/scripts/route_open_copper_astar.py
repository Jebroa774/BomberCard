"""Route DRC-reported open copper endpoints on a common outer layer."""

from __future__ import annotations

import argparse
import heapq
import json
import math
from pathlib import Path
import re
import shutil

import pcbnew

import route_lf_global as maze
import route_plane_fanouts as plane
from route_plane_fanouts import board_rect, existing_obstacles


NET_RE = re.compile(r"\[([^\]]+)\]")
COPPER_ORDER = (pcbnew.F_Cu, pcbnew.In1_Cu, pcbnew.In2_Cu, pcbnew.B_Cu)


def extend_layer_transition(
    route: list[tuple[float, float, int]],
    position: tuple[float, float],
    target_layer: int,
) -> None:
    """Append a manufacturable layer transition at one XY position.

    KiCad represents a non-adjacent 0.30/0.10-mm jump as an undersized
    through-via.  Walk inner-layer transitions one adjacent copper pair at a
    time so ``add_route`` creates stacked microvias instead.  Keep a direct
    F.Cu/B.Cu jump intact because that is an ordinary through-via and callers
    may deliberately select production-size dimensions for it.
    """
    source_layer = route[-1][2]
    if source_layer == target_layer:
        return
    if {source_layer, target_layer} == {pcbnew.F_Cu, pcbnew.B_Cu}:
        route.append((position[0], position[1], target_layer))
        return
    source_index = COPPER_ORDER.index(source_layer)
    target_index = COPPER_ORDER.index(target_layer)
    step = 1 if target_index > source_index else -1
    for index in range(source_index + step, target_index + step, step):
        route.append((position[0], position[1], COPPER_ORDER[index]))


def item_layers(description: str) -> set[int]:
    if (
        "F.Cu - B.Cu" in description
        or "*.Cu" in description
        or description.startswith("Durchsteckpad")
    ):
        return {pcbnew.F_Cu, pcbnew.B_Cu}
    result: set[int] = set()
    if "F.Cu" in description:
        result.add(pcbnew.F_Cu)
    if "B.Cu" in description:
        result.add(pcbnew.B_Cu)
    # KiCad's DRC report uses the user-facing inner-layer names from this
    # project ("GND" and "PWR") rather than the canonical In1/In2 names.
    if "In1.Cu" in description or " auf GND" in description:
        result.add(pcbnew.In1_Cu)
    if "In2.Cu" in description or " auf PWR" in description:
        result.add(pcbnew.In2_Cu)
    return result


def escape_paths_to_vias(
    *,
    net_name: str,
    start: tuple[float, float],
    layer: int,
    route_layer: int,
    edge,
    obstacles,
    maximum_paths: int = 4,
) -> list[tuple[tuple[tuple[float, float], ...], tuple[float, float]]]:
    """Find a few short, DRC-aware escapes from a copper endpoint to a via."""
    local_obstacles = maze.nearby_obstacles(obstacles, start, 11.5)
    spatial = maze.SpatialIndex(local_obstacles)
    # DRC cleanup starts at endpoints which can already be inside another
    # item's clearance halo.  Permit only the first millimetre to leave that
    # pre-existing cage; via placement and the remainder of the route still
    # use the full obstacle set.
    def cages_start_without_touching(obstacle) -> bool:
        route_radius = maze.DIFFERENT_NET_CLEARANCE_MM + maze.TRACK_WIDTH_MM / 2.0
        if obstacle.kind == "pad":
            pad_rect = obstacle.geometry
            return (
                obstacle.net != net_name
                and not pad_rect.contains(start)
                and pad_rect.expanded(route_radius).contains(start)
            )
        if obstacle.kind == "track":
            obstacle_start, obstacle_end, radius, obstacle_layer = obstacle.geometry
            separation = maze.point_segment_distance(start, obstacle_start, obstacle_end)
            return (
                obstacle.net != net_name
                and obstacle_layer == layer
                and radius + 1e-6 < separation <= radius + route_radius + 1e-6
            )
        if obstacle.kind == "via":
            center, radius = obstacle.geometry
            separation = maze.distance(start, center)
            return (
                obstacle.net != net_name
                and radius + 1e-6 < separation <= radius + route_radius + 1e-6
            )
        return False

    start_blockers = {
        id(obstacle)
        for obstacle in local_obstacles
        if cages_start_without_touching(obstacle)
    }
    # Honour the caller's routing grid here as well.  A fixed 0.20-mm escape
    # grid skips the only legal via approaches in several 0.50/0.65-mm-pitch
    # component fields even though the long-section maze is running at 0.05
    # or 0.10 mm.
    step = min(0.20, maze.GRID_MM)
    maximum_radius = 10.0
    queue: list[tuple[float, int, int]] = [(0.0, 0, 0)]
    cost: dict[tuple[int, int], float] = {(0, 0): 0.0}
    previous: dict[tuple[int, int], tuple[int, int]] = {}
    result: list[tuple[tuple[tuple[float, float], ...], tuple[float, float]]] = []
    result_positions: list[tuple[float, float]] = []
    copper_order = (pcbnew.F_Cu, pcbnew.In1_Cu, pcbnew.In2_Cu, pcbnew.B_Cu)
    first_index = copper_order.index(layer)
    second_index = copper_order.index(route_layer)
    via_layers = set(
        copper_order[
            min(first_index, second_index) : max(first_index, second_index) + 1
        ]
    )
    directions = (
        (1, 0),
        (-1, 0),
        (0, 1),
        (0, -1),
        (1, 1),
        (1, -1),
        (-1, 1),
        (-1, -1),
    )
    while queue and len(cost) < 180_000:
        current_cost, ix, iy = heapq.heappop(queue)
        key = (ix, iy)
        if current_cost > cost.get(key, math.inf) + 1e-9:
            continue
        current = (start[0] + ix * step, start[1] + iy * step)
        if current_cost >= 0.60 and maze.signal_via_is_clear(
            net_name=net_name,
            position=current,
            endpoint_pad_ids=set(),
            edge=edge,
            obstacles=spatial.query_point(current),
            via_layers=via_layers,
        ):
            if all(math.dist(current, position) >= 0.90 for position in result_positions):
                keys = [key]
                while keys[-1] != (0, 0):
                    keys.append(previous[keys[-1]])
                keys.reverse()
                points = tuple(
                    maze.simplify_grid_path(
                        [(start[0] + x * step, start[1] + y * step) for x, y in keys]
                    )
                )
                result.append((points, current))
                result_positions.append(current)
                if len(result) >= maximum_paths:
                    return result
        for dx_index, dy_index in directions:
            next_key = (ix + dx_index, iy + dy_index)
            next_position = (
                start[0] + next_key[0] * step,
                start[1] + next_key[1] * step,
            )
            if math.dist(start, next_position) > maximum_radius:
                continue
            segment_obstacles = spatial.query_segment(current, next_position)
            if current_cost < 1.20:
                segment_obstacles = [
                    obstacle
                    for obstacle in segment_obstacles
                    if id(obstacle) not in start_blockers
                ]
            if not maze.track_segment_is_clear(
                net_name=net_name,
                layer=layer,
                start=current,
                end=next_position,
                width_mm=maze.TRACK_WIDTH_MM,
                source_pads=set(),
                edge=edge,
                obstacles=segment_obstacles,
            ):
                continue
            step_cost = step * (math.sqrt(2.0) if dx_index and dy_index else 1.0)
            candidate_cost = current_cost + step_cost
            if candidate_cost + 1e-9 >= cost.get(next_key, math.inf):
                continue
            cost[next_key] = candidate_cost
            previous[next_key] = key
            heapq.heappush(queue, (candidate_cost, *next_key))
    return result


def find_alternate_layer_route(
    *,
    net_name: str,
    start: tuple[float, float],
    end: tuple[float, float],
    native_layer: int,
    route_layer: int,
    edge,
    obstacles,
    expansion: float,
    maximum_escape_paths: int = 2,
) -> tuple[tuple[float, float, int], ...] | None:
    """Escape twice and use another copper layer for the long section."""
    start_escapes = escape_paths_to_vias(
        net_name=net_name,
        start=start,
        layer=native_layer,
        route_layer=route_layer,
        edge=edge,
        obstacles=obstacles,
        maximum_paths=maximum_escape_paths,
    )
    if not start_escapes:
        return None
    end_escapes = escape_paths_to_vias(
        net_name=net_name,
        start=end,
        layer=native_layer,
        route_layer=route_layer,
        edge=edge,
        obstacles=obstacles,
        maximum_paths=maximum_escape_paths,
    )
    pairs = sorted(
        (
            math.dist(start, start_via) + math.dist(start_via, end_via) + math.dist(end_via, end),
            start_path,
            start_via,
            end_path,
            end_via,
        )
        for start_path, start_via in start_escapes
        for end_path, end_via in end_escapes
        if math.dist(start_via, end_via) >= 0.8
    )
    for _, start_path, start_via, end_path, end_via in pairs:
        middle = maze.find_fixed_layer_path(
            net_name=net_name,
            start=start_via,
            end=end_via,
            layer=route_layer,
            endpoint_pad_ids=set(),
            edge=edge,
            obstacles=obstacles,
            expansion=expansion,
        )
        if middle is None:
            continue
        route: list[tuple[float, float, int]] = [
            (x, y, native_layer) for x, y in start_path
        ]
        extend_layer_transition(route, start_via, route_layer)
        route.extend((x, y, route_layer) for x, y in middle[1:])
        extend_layer_transition(route, end_via, native_layer)
        route.extend((x, y, native_layer) for x, y in reversed(end_path[:-1]))
        return tuple(route)
    return None


def find_layer_transition_route(
    *,
    net_name: str,
    start: tuple[float, float],
    end: tuple[float, float],
    start_layer: int,
    end_layer: int,
    edge,
    obstacles,
    expansion: float,
) -> tuple[tuple[float, float, int], ...] | None:
    """Connect endpoints on opposite outer layers using one clear via escape."""
    start_escapes = escape_paths_to_vias(
        net_name=net_name,
        start=start,
        layer=start_layer,
        route_layer=end_layer,
        edge=edge,
        obstacles=obstacles,
        maximum_paths=8,
    )
    end_escapes = escape_paths_to_vias(
        net_name=net_name,
        start=end,
        layer=end_layer,
        route_layer=start_layer,
        edge=edge,
        obstacles=obstacles,
        maximum_paths=8,
    )
    pairs = sorted(
        (
            math.dist(start_via, end_via),
            start_path,
            start_via,
            end_path,
            end_via,
        )
        for start_path, start_via in start_escapes
        for end_path, end_via in end_escapes
    )
    for _, start_path, start_via, end_path, end_via in pairs:
        middle = maze.find_fixed_layer_path(
            net_name=net_name,
            start=start_via,
            end=end_via,
            layer=end_layer,
            endpoint_pad_ids=set(),
            edge=edge,
            obstacles=obstacles,
            expansion=expansion,
        )
        if middle is None:
            continue
        route = [(x, y, start_layer) for x, y in start_path]
        extend_layer_transition(route, start_via, end_layer)
        route.extend((x, y, end_layer) for x, y in middle[1:])
        route.extend((x, y, end_layer) for x, y in reversed(end_path[:-1]))
        return tuple(route)
    for _, start_path, start_via, end_path, end_via in pairs:
        middle = maze.find_fixed_layer_path(
            net_name=net_name,
            start=start_via,
            end=end_via,
            layer=start_layer,
            endpoint_pad_ids=set(),
            edge=edge,
            obstacles=obstacles,
            expansion=expansion,
        )
        if middle is None:
            continue
        route = [(x, y, start_layer) for x, y in start_path]
        route.extend((x, y, start_layer) for x, y in middle[1:])
        extend_layer_transition(route, end_via, end_layer)
        route.extend((x, y, end_layer) for x, y in reversed(end_path[:-1]))
        return tuple(route)
    return None


def main() -> int:
    hardware_dir = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--drc", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--max-distance", type=float, default=35.0)
    parser.add_argument("--max-attempts", type=int, default=40)
    parser.add_argument("--max-routes", type=int, default=8)
    parser.add_argument(
        "--candidate-offset",
        type=int,
        default=0,
        help="skip this many distance-sorted candidates before routing",
    )
    parser.add_argument("--grid", type=float, default=0.20)
    parser.add_argument("--width", type=float, default=0.15)
    parser.add_argument("--clearance", type=float, default=0.20)
    parser.add_argument("--via-diameter", type=float, default=0.30)
    parser.add_argument("--via-drill", type=float, default=0.10)
    parser.add_argument("--expansion", type=float, default=16.0)
    parser.add_argument(
        "--max-search-states",
        type=int,
        default=600_000,
        help="cap the multilayer A* states per candidate",
    )
    parser.add_argument(
        "--escape-paths",
        type=int,
        default=2,
        help="maximum via escapes retained per endpoint for alternate-layer routing",
    )
    parser.add_argument(
        "--net",
        action="append",
        help="Route only this net; may be repeated",
    )
    parser.add_argument(
        "--start-uuid",
        help="explicit start pad UUID; requires --end-uuid and exactly one --net",
    )
    parser.add_argument(
        "--end-uuid",
        help="explicit end pad UUID; requires --start-uuid and exactly one --net",
    )
    parser.add_argument(
        "--start-position",
        help="explicit start as x,y,layer (for example 48.805,23.205,B.Cu)",
    )
    parser.add_argument(
        "--end-position",
        help="explicit end as x,y,layer; requires --start-position and one --net",
    )
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--pad-obstacles-only", action="store_true")
    parser.add_argument(
        "--pads-only",
        action="store_true",
        help="only route DRC edges whose two reported endpoints are pads",
    )
    parser.add_argument(
        "--multilayer-pads",
        action="store_true",
        help="use the full F.Cu/B.Cu multi-via maze for pad-to-pad edges",
    )
    parser.add_argument(
        "--ignore-endpoint-cages",
        action="store_true",
        help="ignore obstacle halos that already contain a reported endpoint",
    )
    parser.add_argument(
        "--ignore-all-endpoint-cages",
        action="store_true",
        help=(
            "also ignore nearby pad/via halos at the two reported endpoints; "
            "keepouts remain enforced and the result still requires full DRC"
        ),
    )
    parser.add_argument("--all-edges", action="store_true")
    parser.add_argument("--allow-vias", action="store_true")
    parser.add_argument(
        "--alternate-inner",
        action="store_true",
        help=(
            "when a same-outer-layer edge is blocked, also try In2.Cu and "
            "In1.Cu for the long section after DRC-aware endpoint escapes"
        ),
    )
    parser.add_argument(
        "--prefer-alternate-layer",
        action="store_true",
        help="try the via escape and alternate layers before a common native layer",
    )
    parser.add_argument(
        "--direct-endpoint-vias",
        action="store_true",
        help=(
            "for two endpoints on one outer layer, try through-vias directly "
            "at both reported endpoints and route the middle on the opposite outer layer"
        ),
    )
    parser.add_argument(
        "--single-endpoint-via",
        action="store_true",
        help=(
            "for an outer-layer SMD pad connected to a through-hole pad, "
            "place one via at the SMD endpoint and finish on a selected inner layer"
        ),
    )
    parser.add_argument(
        "--prefer-ground-inner",
        action="store_true",
        help="when inner alternatives are enabled, try In1.Cu before In2.Cu",
    )
    parser.add_argument(
        "--alternate-layer",
        choices=("auto", "F.Cu", "In1.Cu", "In2.Cu", "B.Cu"),
        default="auto",
        help="restrict alternate-layer routing to one selected copper layer",
    )
    parser.add_argument(
        "--routing-layer-set",
        choices=("outer", "f-in1", "b-in2", "all"),
        default="outer",
        help="layers available to the full multilayer pad router",
    )
    parser.add_argument(
        "--allow-power-selected",
        action="store_true",
        help="allow explicitly selected power nets that are normally skipped",
    )
    parser.add_argument("--skip-zone-fill", action="store_true")
    parser.add_argument(
        "--save-closest-partial",
        action="store_true",
        help="candidate mode: save the closest fully checked multilayer path on failure",
    )
    parser.add_argument(
        "--adjacent-vias-only",
        action="store_true",
        help="restrict layer changes to adjacent-layer microvias",
    )
    parser.add_argument(
        "--min-moves-between-vias",
        type=int,
        default=0,
        help="minimum grid moves between consecutive layer transitions",
    )
    args = parser.parse_args()

    output = args.output.resolve()
    if output == (hardware_dir / "PocketLab-Card.kicad_pcb").resolve():
        raise RuntimeError("Refusing to overwrite the authoritative PCB")
    if output.exists() and not args.force:
        raise RuntimeError(f"Output exists; use --force: {output}")

    board = pcbnew.LoadBoard(str(args.input.resolve()))
    report = json.loads(args.drc.read_text(encoding="utf-8"))
    selected_nets = set(args.net or ())
    if bool(args.start_uuid) != bool(args.end_uuid):
        raise RuntimeError("--start-uuid and --end-uuid must be supplied together")
    if bool(args.start_position) != bool(args.end_position):
        raise RuntimeError(
            "--start-position and --end-position must be supplied together"
        )
    if args.start_uuid and args.start_position:
        raise RuntimeError("explicit UUIDs and explicit positions are mutually exclusive")
    explicit_endpoints = bool(args.start_uuid or args.start_position)
    if explicit_endpoints and len(selected_nets) != 1:
        raise RuntimeError("explicit endpoints require exactly one --net")

    routing_layers = {pcbnew.F_Cu, pcbnew.In1_Cu, pcbnew.In2_Cu, pcbnew.B_Cu}
    pads_by_uuid = {
        pad.m_Uuid.AsString(): pad
        for footprint in board.GetFootprints()
        for pad in footprint.Pads()
    }
    candidates = []
    report_items = () if explicit_endpoints else report.get("unconnected_items", [])
    for open_item in report_items:
        items = open_item.get("items", [])
        if len(items) != 2:
            continue
        if args.pads_only and not all(
            item.get("description", "").startswith(("Pad ", "Durchsteckpad "))
            for item in items
        ):
            continue
        match = NET_RE.search(items[0].get("description", ""))
        if match is None:
            continue
        net_name = match.group(1)
        if selected_nets and net_name not in selected_nets:
            continue
        if (
            net_name in {"/GND", "/+3V3", "/+5V_RAW", "/+5V_AUX", "/VSYS"}
            and not (args.allow_power_selected and net_name in selected_nets)
        ):
            continue
        start_layers = item_layers(items[0].get("description", ""))
        end_layers = item_layers(items[1].get("description", ""))
        layers = start_layers & end_layers
        has_layer_transition = bool(
            args.allow_vias
            and not layers
            and start_layers & routing_layers
            and end_layers & routing_layers
        )
        if not layers and not has_layer_transition:
            continue
        start = (float(items[0]["pos"]["x"]), float(items[0]["pos"]["y"]))
        end = (float(items[1]["pos"]["x"]), float(items[1]["pos"]["y"]))
        distance = math.dist(start, end)
        if distance <= args.max_distance:
            candidates.append(
                (
                    distance,
                    net_name,
                    start,
                    end,
                    layers,
                    start_layers,
                    end_layers,
                    items[0].get("uuid", ""),
                    items[1].get("uuid", ""),
                )
            )
    if args.start_uuid:
        start_pad = pads_by_uuid.get(args.start_uuid)
        end_pad = pads_by_uuid.get(args.end_uuid)
        if start_pad is None or end_pad is None:
            missing = [
                uuid
                for uuid, pad in (
                    (args.start_uuid, start_pad),
                    (args.end_uuid, end_pad),
                )
                if pad is None
            ]
            raise RuntimeError(f"explicit pad UUID not found: {', '.join(missing)}")
        net_name = next(iter(selected_nets))
        for label, pad in (("start", start_pad), ("end", end_pad)):
            if pad.GetNetname() != net_name:
                raise RuntimeError(
                    f"explicit {label} pad belongs to {pad.GetNetname()}, not {net_name}"
                )
        start_pos = start_pad.GetPosition()
        end_pos = end_pad.GetPosition()
        start = (pcbnew.ToMM(start_pos.x), pcbnew.ToMM(start_pos.y))
        end = (pcbnew.ToMM(end_pos.x), pcbnew.ToMM(end_pos.y))
        start_layers = set(start_pad.GetLayerSet().Seq()) & routing_layers
        end_layers = set(end_pad.GetLayerSet().Seq()) & routing_layers
        layers = start_layers & end_layers
        if not layers and not args.allow_vias:
            raise RuntimeError("explicit endpoint pads require vias but --allow-vias is off")
        candidates.append(
            (
                math.dist(start, end),
                net_name,
                start,
                end,
                layers,
                start_layers,
                end_layers,
                args.start_uuid,
                args.end_uuid,
            )
        )
    elif args.start_position:
        layer_ids = {
            "F.Cu": pcbnew.F_Cu,
            "In1.Cu": pcbnew.In1_Cu,
            "In2.Cu": pcbnew.In2_Cu,
            "B.Cu": pcbnew.B_Cu,
        }

        def parse_position(value: str) -> tuple[tuple[float, float], set[int]]:
            parts = [part.strip() for part in value.split(",")]
            if len(parts) != 3 or parts[2] not in layer_ids:
                raise RuntimeError("explicit positions must use x,y,F.Cu|In1.Cu|In2.Cu|B.Cu")
            return (float(parts[0]), float(parts[1])), {layer_ids[parts[2]]}

        start, start_layers = parse_position(args.start_position)
        end, end_layers = parse_position(args.end_position)
        layers = start_layers & end_layers
        candidates.append(
            (
                math.dist(start, end),
                next(iter(selected_nets)),
                start,
                end,
                layers,
                start_layers,
                end_layers,
                "",
                "",
            )
        )
    candidates.sort(key=lambda item: (item[0], item[1]))
    if args.candidate_offset:
        candidates = candidates[args.candidate_offset :]

    maze.GRID_MM = args.grid
    maze.TRACK_WIDTH_MM = args.width
    maze.VIA_DIAMETER_MM = args.via_diameter
    maze.VIA_DRILL_MM = args.via_drill
    maze.DIFFERENT_NET_CLEARANCE_MM = args.clearance
    plane.DIFFERENT_NET_CLEARANCE_MM = args.clearance
    maze.ROUTE_EXPANSION_MM = args.expansion
    maze.MAX_ROUTE_SEARCH_STATES = args.max_search_states
    maze.MAX_FIXED_LAYER_SEARCH_STATES = args.max_search_states
    maze.ROUTING_LAYERS = {
        "outer": (pcbnew.F_Cu, pcbnew.B_Cu),
        "f-in1": (pcbnew.F_Cu, pcbnew.In1_Cu),
        "b-in2": (pcbnew.B_Cu, pcbnew.In2_Cu),
        "all": (pcbnew.F_Cu, pcbnew.In1_Cu, pcbnew.In2_Cu, pcbnew.B_Cu),
    }[args.routing_layer_set]
    maze.ADJACENT_LAYER_VIAS_ONLY = args.adjacent_vias_only
    maze.MIN_MOVES_BETWEEN_VIAS = args.min_moves_between_vias
    maze.AVOID_L3_ZONE_POLYS = ()
    edge = board_rect(board)
    obstacles = existing_obstacles(board)
    used_nets: set[str] = set()
    attempts = 0
    routed = 0

    def save_checkpoint() -> None:
        # Long maze searches may be stopped by an outer time limit.  Persist
        # every completed route so a later difficult candidate cannot discard
        # the useful work already completed in this batch.
        pcbnew.SaveBoard(str(output), board)

    for (
        distance,
        net_name,
        start,
        end,
        layers,
        start_layers,
        end_layers,
        start_uuid,
        end_uuid,
    ) in candidates:
        if routed >= args.max_routes or attempts >= args.max_attempts:
            break
        if not args.all_edges and net_name in used_nets:
            continue
        attempts += 1
        # Existing copper of the net being completed is a valid destination,
        # not an obstacle.  Keeping it in the maze cages the search at pads
        # and short existing stubs, which made every candidate fail before a
        # path could leave its endpoint.  Other-net copper and keepouts remain
        # fully clearance-checked; the completed batch is still accepted only
        # after KiCad DRC.
        routing_obstacles = [
            obstacle
            for obstacle in obstacles
            if obstacle.net != net_name
            and (
                not args.pad_obstacles_only
                or obstacle.kind in {"pad", "keepout", "copper_graphic"}
            )
        ]
        if args.ignore_endpoint_cages or args.ignore_all_endpoint_cages:
            routing_obstacles = [
                obstacle
                for obstacle in routing_obstacles
                if obstacle.kind == "keepout"
                or (
                    not args.ignore_all_endpoint_cages
                    and obstacle.kind in {"pad", "via"}
                )
                or (
                    not maze.obstacle_rect(obstacle)
                    .expanded(args.clearance + args.width / 2.0)
                    .contains(start)
                    and not maze.obstacle_rect(obstacle)
                    .expanded(args.clearance + args.width / 2.0)
                    .contains(end)
                )
            ]
        if args.direct_endpoint_vias and len(layers) == 1:
            native_layer = next(iter(layers))
            if native_layer in {pcbnew.F_Cu, pcbnew.B_Cu}:
                start_pad = pads_by_uuid.get(start_uuid)
                end_pad = pads_by_uuid.get(end_uuid)
                if args.alternate_layer != "auto":
                    route_layer = {
                        "F.Cu": pcbnew.F_Cu,
                        "In1.Cu": pcbnew.In1_Cu,
                        "In2.Cu": pcbnew.In2_Cu,
                        "B.Cu": pcbnew.B_Cu,
                    }[args.alternate_layer]
                else:
                    route_layer = (
                        pcbnew.B_Cu if native_layer == pcbnew.F_Cu else pcbnew.F_Cu
                    )
                if route_layer == native_layer:
                    route_layer = (
                        pcbnew.B_Cu if native_layer == pcbnew.F_Cu else pcbnew.F_Cu
                    )
                via_layers = {native_layer, route_layer}
                endpoint_vias_clear = all(
                    maze.signal_via_is_clear(
                        net_name=net_name,
                        position=position,
                        endpoint_pad_ids=set(),
                        edge=edge,
                        obstacles=routing_obstacles,
                        via_layers=via_layers,
                    )
                    for position in (start, end)
                )
                if not endpoint_vias_clear:
                    for label, position in (("start", start), ("end", end)):
                        clear_here = maze.signal_via_is_clear(
                            net_name=net_name,
                            position=position,
                            endpoint_pad_ids=set(),
                            edge=edge,
                            obstacles=routing_obstacles,
                            via_layers=via_layers,
                        )
                        print(
                            f"DIRECT-ENDPOINT-VIA {net_name}: {label} "
                            f"{position[0]:.3f},{position[1]:.3f} clear={clear_here}",
                            flush=True,
                        )
                        if not clear_here:
                            for obstacle in routing_obstacles:
                                if not maze.signal_via_is_clear(
                                    net_name=net_name,
                                    position=position,
                                    endpoint_pad_ids=set(),
                                    edge=edge,
                                    obstacles=[obstacle],
                                    via_layers=via_layers,
                                ):
                                    owner = obstacle.owner
                                    owner_uuid = (
                                        owner.m_Uuid.AsString()
                                        if owner is not None and hasattr(owner, "m_Uuid")
                                        else ""
                                    )
                                    print(
                                        f"  VIA_BLOCKER kind={obstacle.kind} "
                                        f"net={obstacle.net} uuid={owner_uuid}",
                                        flush=True,
                                    )
                if endpoint_vias_clear:
                    middle = maze.find_fixed_layer_path(
                        net_name=net_name,
                        start=start,
                        end=end,
                        layer=route_layer,
                        endpoint_pad_ids=set(),
                        edge=edge,
                        obstacles=routing_obstacles,
                        expansion=args.expansion,
                    )
                    middle_route = None
                    if middle is None and start_pad is not None and end_pad is not None:
                        middle_route = maze.find_route(
                            net_name=net_name,
                            start_pad=start_pad,
                            end_pad=end_pad,
                            edge=edge,
                            obstacles=routing_obstacles,
                            start_override=start,
                            end_override=end,
                            start_layer_override=route_layer,
                            end_layer_override=route_layer,
                        )
                    if middle is not None or middle_route is not None:
                        direct_route_list = [(start[0], start[1], native_layer)]
                        extend_layer_transition(direct_route_list, start, route_layer)
                        if middle is not None:
                            direct_route_list.extend(
                                (x, y, route_layer) for x, y in middle[1:]
                            )
                        else:
                            direct_route_list.extend(middle_route[1:])
                        extend_layer_transition(direct_route_list, end, native_layer)
                        direct_route = tuple(direct_route_list)
                        tracks, vias = maze.add_route(
                            board, net_name, direct_route, obstacles
                        )
                        routed += 1
                        if not args.all_edges:
                            used_nets.add(net_name)
                        save_checkpoint()
                        print(
                            f"ROUTED {net_name} ENDPOINT-VIAS "
                            f"distance={distance:.2f} tracks={tracks} vias={vias}",
                            flush=True,
                        )
                        continue
        if args.single_endpoint_via and len(layers) == 1:
            native_layer = next(iter(layers))
            if native_layer in {pcbnew.F_Cu, pcbnew.B_Cu}:
                start_pad = pads_by_uuid.get(start_uuid)
                end_pad = pads_by_uuid.get(end_uuid)
                if args.alternate_layer != "auto":
                    route_layers = [{
                        "F.Cu": pcbnew.F_Cu,
                        "In1.Cu": pcbnew.In1_Cu,
                        "In2.Cu": pcbnew.In2_Cu,
                        "B.Cu": pcbnew.B_Cu,
                    }[args.alternate_layer]]
                else:
                    route_layers = [pcbnew.In2_Cu, pcbnew.In1_Cu]
                endpoint_pairs = []
                if start_pad is not None and end_pad is not None:
                    for route_layer in route_layers:
                        if (
                            start_pad.IsOnLayer(native_layer)
                            and not start_pad.IsOnLayer(route_layer)
                            and end_pad.IsOnLayer(route_layer)
                        ):
                            endpoint_pairs.append((start, end, route_layer, False))
                        if (
                            end_pad.IsOnLayer(native_layer)
                            and not end_pad.IsOnLayer(route_layer)
                            and start_pad.IsOnLayer(route_layer)
                        ):
                            endpoint_pairs.append((end, start, route_layer, True))
                print(
                    f"SINGLE-ENDPOINT-VIA {net_name}: native={native_layer} "
                    f"pairs={len(endpoint_pairs)} start_pad={start_pad is not None} "
                    f"end_pad={end_pad is not None}",
                    flush=True,
                )
                single_route = None
                for smd_position, tht_position, route_layer, reverse_route in endpoint_pairs:
                    copper_order = tuple(COPPER_ORDER)
                    first_index = copper_order.index(native_layer)
                    second_index = copper_order.index(route_layer)
                    via_layers = set(
                        copper_order[
                            min(first_index, second_index) : max(first_index, second_index) + 1
                        ]
                    )
                    via_clear = maze.signal_via_is_clear(
                        net_name=net_name,
                        position=smd_position,
                        endpoint_pad_ids=set(),
                        edge=edge,
                        obstacles=routing_obstacles,
                        via_layers=via_layers,
                    )
                    print(
                        f"SINGLE-ENDPOINT-VIA {net_name}: layer={route_layer} "
                        f"via={smd_position[0]:.3f},{smd_position[1]:.3f} clear={via_clear}",
                        flush=True,
                    )
                    if not via_clear:
                        for obstacle in routing_obstacles:
                            if not maze.signal_via_is_clear(
                                net_name=net_name,
                                position=smd_position,
                                endpoint_pad_ids=set(),
                                edge=edge,
                                obstacles=[obstacle],
                                via_layers=via_layers,
                            ):
                                owner = obstacle.owner
                                owner_uuid = (
                                    owner.m_Uuid.AsString()
                                    if owner is not None and hasattr(owner, "m_Uuid")
                                    else ""
                                )
                                print(
                                    f"  VIA_BLOCKER kind={obstacle.kind} net={obstacle.net} "
                                    f"uuid={owner_uuid}",
                                    flush=True,
                                )
                        continue
                    middle = maze.find_fixed_layer_path(
                        net_name=net_name,
                        start=smd_position,
                        end=tht_position,
                        layer=route_layer,
                        endpoint_pad_ids=set(),
                        edge=edge,
                        obstacles=routing_obstacles,
                        expansion=args.expansion,
                    )
                    route_list = [(smd_position[0], smd_position[1], native_layer)]
                    extend_layer_transition(route_list, smd_position, route_layer)
                    if middle is not None:
                        route_list.extend((x, y, route_layer) for x, y in middle[1:])
                    else:
                        middle_route = maze.find_route(
                            net_name=net_name,
                            start_pad=start_pad if not reverse_route else end_pad,
                            end_pad=end_pad if not reverse_route else start_pad,
                            edge=edge,
                            obstacles=routing_obstacles,
                            start_override=smd_position,
                            end_override=tht_position,
                            start_layer_override=route_layer,
                            end_layer_override=route_layer,
                        )
                        if middle_route is None:
                            continue
                        route_list.extend(middle_route[1:])
                    if reverse_route:
                        route_list.reverse()
                    single_route = tuple(route_list)
                    break
                if single_route is not None:
                    tracks, vias = maze.add_route(
                        board, net_name, single_route, obstacles
                    )
                    routed += 1
                    if not args.all_edges:
                        used_nets.add(net_name)
                    save_checkpoint()
                    print(
                        f"ROUTED {net_name} SINGLE-ENDPOINT-VIA "
                        f"distance={distance:.2f} tracks={tracks} vias={vias}",
                        flush=True,
                    )
                    continue
        if (
            args.prefer_alternate_layer
            and args.allow_vias
            and len(layers) == 1
        ):
            native_layer = next(iter(layers))
            if native_layer in {pcbnew.F_Cu, pcbnew.B_Cu}:
                if args.alternate_layer != "auto":
                    alternate_layers = [{
                        "F.Cu": pcbnew.F_Cu,
                        "In1.Cu": pcbnew.In1_Cu,
                        "In2.Cu": pcbnew.In2_Cu,
                        "B.Cu": pcbnew.B_Cu,
                    }[args.alternate_layer]]
                else:
                    alternate_layers = [
                        pcbnew.B_Cu if native_layer == pcbnew.F_Cu else pcbnew.F_Cu,
                    ]
                if args.alternate_inner and args.alternate_layer == "auto":
                    inner_layers = (
                        [pcbnew.In1_Cu, pcbnew.In2_Cu]
                        if args.prefer_ground_inner
                        else [pcbnew.In2_Cu, pcbnew.In1_Cu]
                    )
                    alternate_layers = inner_layers + alternate_layers
                preferred_route = None
                for route_layer in alternate_layers:
                    preferred_route = find_alternate_layer_route(
                        net_name=net_name,
                        start=start,
                        end=end,
                        native_layer=native_layer,
                        route_layer=route_layer,
                        edge=edge,
                        obstacles=routing_obstacles,
                        expansion=args.expansion,
                        maximum_escape_paths=args.escape_paths,
                    )
                    if preferred_route is not None:
                        break
                if preferred_route is not None:
                    tracks, vias = maze.add_route(
                        board, net_name, preferred_route, obstacles
                    )
                    routed += 1
                    if not args.all_edges:
                        used_nets.add(net_name)
                    save_checkpoint()
                    print(
                        f"ROUTED {net_name} VIA-PREFERRED distance={distance:.2f} "
                        f"tracks={tracks} vias={vias}",
                        flush=True,
                    )
                    continue
        if args.multilayer_pads:
            start_pad = pads_by_uuid.get(start_uuid)
            end_pad = pads_by_uuid.get(end_uuid)
            if start_pad is not None and end_pad is not None:
                # Through-hole pads are present on both outer copper layers,
                # so ``pad_layer`` intentionally returns no single layer for
                # them.  For a THT-to-SMD open edge the other endpoint makes
                # the intended outer start/end layer unambiguous.  Supplying
                # that layer override lets the full multilayer maze route
                # these valid pairs instead of skipping them outright.
                route_layer_order = tuple(maze.ROUTING_LAYERS)

                def endpoint_layer_override(
                    pad: pcbnew.PAD,
                    endpoint_layers: set[int],
                    other_layers: set[int],
                ) -> int | None:
                    if maze.pad_layer(pad) is not None:
                        return None
                    available = endpoint_layers & set(route_layer_order)
                    preferred = available & other_layers
                    choices = preferred or available
                    return next(
                        (layer for layer in route_layer_order if layer in choices),
                        None,
                    )

                start_layer_override = endpoint_layer_override(
                    start_pad, start_layers, end_layers
                )
                end_layer_override = endpoint_layer_override(
                    end_pad, end_layers, start_layers
                )
                try:
                    route = maze.find_route(
                        net_name=net_name,
                        start_pad=start_pad,
                        end_pad=end_pad,
                        edge=edge,
                        obstacles=routing_obstacles,
                        start_layer_override=start_layer_override,
                        end_layer_override=end_layer_override,
                        allow_closest_partial=args.save_closest_partial,
                    )
                except RuntimeError as error:
                    print(f"MULTI_SKIPPED {net_name}: {error}", flush=True)
                    route = None
                if route is not None:
                    tracks, vias = maze.add_route(board, net_name, route, obstacles)
                    routed += 1
                    if not args.all_edges:
                        used_nets.add(net_name)
                    save_checkpoint()
                    print(
                        f"ROUTED {net_name} MULTI distance={distance:.2f} "
                        f"tracks={tracks} vias={vias}",
                        flush=True,
                    )
                    continue
        for layer in sorted(layers, key=lambda value: value != pcbnew.B_Cu):
            result = maze.find_fixed_layer_path_to_goals(
                net_name=net_name,
                start=start,
                ends=(end,),
                layer=layer,
                endpoint_pad_ids=set(),
                edge=edge,
                obstacles=routing_obstacles,
                expansion=args.expansion,
                debug_label=net_name,
            )
            if result is None:
                continue
            points, _ = result
            route = tuple((x, y, layer) for x, y in points)
            tracks, vias = maze.add_route(board, net_name, route, obstacles)
            routed += 1
            if not args.all_edges:
                used_nets.add(net_name)
            save_checkpoint()
            print(
                f"ROUTED {net_name} {board.GetLayerName(layer)} "
                f"distance={distance:.2f} tracks={tracks} vias={vias}",
                flush=True,
            )
            break
        else:
            route = None
            if args.allow_vias and not layers:
                routing_layers = {pcbnew.F_Cu, pcbnew.B_Cu}
                transition_pairs = [
                    (start_layer, end_layer)
                    for start_layer in start_layers & routing_layers
                    for end_layer in end_layers & routing_layers
                    if start_layer != end_layer
                ]
                for start_layer, end_layer in transition_pairs:
                    route = find_layer_transition_route(
                        net_name=net_name,
                        start=start,
                        end=end,
                        start_layer=start_layer,
                        end_layer=end_layer,
                        edge=edge,
                        obstacles=routing_obstacles,
                        expansion=args.expansion,
                    )
                    if route is not None:
                        break
            if args.allow_vias and len(layers) == 1:
                native_layer = next(iter(layers))
                if native_layer in {pcbnew.F_Cu, pcbnew.B_Cu}:
                    if args.alternate_layer != "auto":
                        alternate_layers = [{
                            "F.Cu": pcbnew.F_Cu,
                            "In1.Cu": pcbnew.In1_Cu,
                            "In2.Cu": pcbnew.In2_Cu,
                            "B.Cu": pcbnew.B_Cu,
                        }[args.alternate_layer]]
                    else:
                        alternate_layers = [
                            pcbnew.B_Cu if native_layer == pcbnew.F_Cu else pcbnew.F_Cu,
                        ]
                    if args.alternate_inner and args.alternate_layer == "auto":
                        # Prefer the power plane over the ground plane so the
                        # continuous return reference is disturbed only when
                        # no other checked candidate exists.
                        alternate_layers.extend(
                            (pcbnew.In1_Cu, pcbnew.In2_Cu)
                            if args.prefer_ground_inner
                            else (pcbnew.In2_Cu, pcbnew.In1_Cu)
                        )
                    for route_layer in alternate_layers:
                        route = find_alternate_layer_route(
                            net_name=net_name,
                            start=start,
                            end=end,
                            native_layer=native_layer,
                            route_layer=route_layer,
                            edge=edge,
                            obstacles=routing_obstacles,
                            expansion=args.expansion,
                            maximum_escape_paths=args.escape_paths,
                        )
                        if route is not None:
                            break
            if route is None:
                print(f"FAILED {net_name} distance={distance:.2f}", flush=True)
                continue
            tracks, vias = maze.add_route(board, net_name, route, obstacles)
            routed += 1
            if not args.all_edges:
                used_nets.add(net_name)
            save_checkpoint()
            print(
                f"ROUTED {net_name} VIA distance={distance:.2f} "
                f"tracks={tracks} vias={vias}",
                flush=True,
            )

    if not args.skip_zone_fill:
        pcbnew.ZONE_FILLER(board).Fill(board.Zones())
    pcbnew.SaveBoard(str(output), board)
    for suffix in (".kicad_pro", ".kicad_dru"):
        shutil.copyfile(hardware_dir / f"PocketLab-Card{suffix}", output.with_suffix(suffix))
    print(f"SAVED routes={routed} attempts={attempts}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
