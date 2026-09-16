"""Redistribute candidate-added direct tracks across copper layers.

The fast connectivity pass places a through via at both ends of each added
direct track.  Such a track can therefore be moved to another copper layer
without changing connectivity.  This tool compares the candidate with a
cleaner base board, builds a geometric conflict graph for only those added
tracks, and assigns layers greedily while preserving all older routing.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import json
import math
from pathlib import Path
import shutil

import pcbnew

from prune_new_drc_conflicts import geometry_key, uuid_text


COPPER_LAYERS = (pcbnew.F_Cu, pcbnew.In1_Cu, pcbnew.In2_Cu, pcbnew.B_Cu)
DRC_TYPES = {"clearance", "shorting_items", "tracks_crossing"}


def mm_point(vector: pcbnew.VECTOR2I) -> tuple[float, float]:
    return pcbnew.ToMM(vector.x), pcbnew.ToMM(vector.y)


def orientation(
    first: tuple[float, float],
    second: tuple[float, float],
    third: tuple[float, float],
) -> float:
    return (second[0] - first[0]) * (third[1] - first[1]) - (
        second[1] - first[1]
    ) * (third[0] - first[0])


def proper_intersection(
    a: tuple[float, float],
    b: tuple[float, float],
    c: tuple[float, float],
    d: tuple[float, float],
) -> bool:
    return orientation(a, b, c) * orientation(a, b, d) < -1e-9 and orientation(
        c, d, a
    ) * orientation(c, d, b) < -1e-9


def point_segment_distance(
    point: tuple[float, float],
    start: tuple[float, float],
    end: tuple[float, float],
) -> float:
    dx = end[0] - start[0]
    dy = end[1] - start[1]
    length_squared = dx * dx + dy * dy
    if length_squared < 1e-12:
        return math.dist(point, start)
    position = max(
        0.0,
        min(
            1.0,
            ((point[0] - start[0]) * dx + (point[1] - start[1]) * dy)
            / length_squared,
        ),
    )
    projection = (start[0] + position * dx, start[1] + position * dy)
    return math.dist(point, projection)


def segment_distance(
    a: tuple[float, float],
    b: tuple[float, float],
    c: tuple[float, float],
    d: tuple[float, float],
) -> float:
    if proper_intersection(a, b, c, d):
        return 0.0
    return min(
        point_segment_distance(a, c, d),
        point_segment_distance(b, c, d),
        point_segment_distance(c, a, b),
        point_segment_distance(d, a, b),
    )


def occupied_layers(item: pcbnew.BOARD_ITEM) -> set[int]:
    if isinstance(item, pcbnew.PCB_VIA):
        return set(COPPER_LAYERS)
    if isinstance(item, pcbnew.PCB_TRACK):
        return {item.GetLayer()}
    if isinstance(item, pcbnew.PAD):
        return {layer for layer in COPPER_LAYERS if item.IsOnLayer(layer)}
    if isinstance(item, pcbnew.ZONE):
        return {layer for layer in COPPER_LAYERS if item.IsOnLayer(layer)}
    return set()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", type=Path)
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--drc", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--allow-inner", action="store_true")
    parser.add_argument(
        "--shorts-only",
        action="store_true",
        help="consider every shorting track with all-layer anchors, without a base board",
    )
    parser.add_argument("--clearance", type=float, default=0.16)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    hardware_dir = Path(__file__).resolve().parent.parent
    authoritative = (hardware_dir / "PocketLab-Card.kicad_pcb").resolve()
    protected = {authoritative, args.candidate.resolve()}
    if args.base:
        protected.add(args.base.resolve())
    if args.output.resolve() in protected:
        raise RuntimeError("output must be a separate non-authoritative board")
    if args.output.exists() and not args.force:
        raise RuntimeError(f"output exists: {args.output}")

    candidate = pcbnew.LoadBoard(str(args.candidate.resolve()))
    report = json.loads(args.drc.read_text(encoding="utf-8"))
    if args.shorts_only:
        short_track_ids = {
            item.get("uuid", "")
            for violation in report.get("violations", [])
            if violation.get("type") == "shorting_items"
            for item in violation.get("items", [])
            if item.get("description", "").startswith("Leiterbahn ")
        }
        candidate_added_tracks = {
            uuid_text(item): item
            for item in candidate.GetTracks()
            if not isinstance(item, pcbnew.PCB_VIA)
            and uuid_text(item) in short_track_ids
        }
    else:
        if not args.base:
            raise RuntimeError("--base is required unless --shorts-only is used")
        base = pcbnew.LoadBoard(str(args.base.resolve()))
        base_keys = {geometry_key(item) for item in base.GetTracks()}
        candidate_added_tracks = {
            uuid_text(item): item
            for item in candidate.GetTracks()
            if not isinstance(item, pcbnew.PCB_VIA)
            and geometry_key(item) not in base_keys
        }
    all_layer_anchors: set[tuple[int, int, int]] = set()
    for item in candidate.GetTracks():
        if isinstance(item, pcbnew.PCB_VIA):
            position = item.GetPosition()
            all_layer_anchors.add((position.x, position.y, item.GetNetCode()))
    for footprint in candidate.GetFootprints():
        for pad in footprint.Pads():
            if len(occupied_layers(pad)) == len(COPPER_LAYERS):
                position = pad.GetPosition()
                all_layer_anchors.add((position.x, position.y, pad.GetNetCode()))
    new_tracks = {
        item_uuid: item
        for item_uuid, item in candidate_added_tracks.items()
        if (
            item.GetStart().x,
            item.GetStart().y,
            item.GetNetCode(),
        )
        in all_layer_anchors
        and (
            item.GetEnd().x,
            item.GetEnd().y,
            item.GetNetCode(),
        )
        in all_layer_anchors
    }
    if not new_tracks:
        raise RuntimeError("candidate contains no added tracks")

    all_items: dict[str, pcbnew.BOARD_ITEM] = {
        uuid_text(item): item for item in candidate.GetTracks()
    }
    for footprint in candidate.GetFootprints():
        for pad in footprint.Pads():
            all_items[uuid_text(pad)] = pad
    for zone in candidate.Zones():
        all_items[uuid_text(zone)] = zone

    pair_weights: Counter[tuple[str, str]] = Counter()
    fixed_penalties: dict[str, Counter[int]] = defaultdict(Counter)
    for violation in report.get("violations", []):
        if violation.get("type") not in DRC_TYPES:
            continue
        uuids = list(
            dict.fromkeys(item.get("uuid", "") for item in violation.get("items", []))
        )
        direct = [item_uuid for item_uuid in uuids if item_uuid in new_tracks]
        for index, first in enumerate(direct):
            for second in direct[index + 1 :]:
                pair_weights[tuple(sorted((first, second)))] += 4
            for other_uuid in uuids:
                if other_uuid == first or other_uuid in new_tracks:
                    continue
                other = all_items.get(other_uuid)
                if other is None:
                    continue
                layers = occupied_layers(other)
                if len(layers) < len(COPPER_LAYERS):
                    for layer in layers:
                        fixed_penalties[first][layer] += 8

    # Add every geometric crossing between added tracks, including crossings
    # that were hidden because the previous tracks were on different layers.
    new_list = list(new_tracks.items())
    for index, (first_uuid, first) in enumerate(new_list):
        first_start, first_end = mm_point(first.GetStart()), mm_point(first.GetEnd())
        for second_uuid, second in new_list[index + 1 :]:
            if first.GetNetCode() == second.GetNetCode():
                continue
            second_start, second_end = mm_point(second.GetStart()), mm_point(second.GetEnd())
            spacing = (
                pcbnew.ToMM(first.GetWidth() + second.GetWidth()) / 2.0
                + args.clearance
            )
            distance = segment_distance(
                first_start, first_end, second_start, second_end
            )
            if distance < spacing:
                pair_weights[tuple(sorted((first_uuid, second_uuid)))] += 3

    # Penalize predicted collisions with older tracks and pads on every layer
    # that may be selected.  Inner planes are refilled after reassignment, so
    # only fixed inner tracks need to be treated as geometric obstacles here.
    older_tracks = [
        item
        for item in candidate.GetTracks()
        if not isinstance(item, pcbnew.PCB_VIA)
        and uuid_text(item) not in new_tracks
        and item.GetLayer() in COPPER_LAYERS
    ]
    pads = [pad for footprint in candidate.GetFootprints() for pad in footprint.Pads()]
    for item_uuid, item in new_tracks.items():
        start, end = mm_point(item.GetStart()), mm_point(item.GetEnd())
        width = pcbnew.ToMM(item.GetWidth())
        for older in older_tracks:
            if older.GetNetCode() == item.GetNetCode():
                continue
            spacing = (
                width / 2.0
                + pcbnew.ToMM(older.GetWidth()) / 2.0
                + args.clearance
            )
            if (
                segment_distance(
                    start,
                    end,
                    mm_point(older.GetStart()),
                    mm_point(older.GetEnd()),
                )
                < spacing
            ):
                fixed_penalties[item_uuid][older.GetLayer()] += 5
        for pad in pads:
            if pad.GetNetCode() == item.GetNetCode():
                continue
            pad_layers = occupied_layers(pad)
            if not pad_layers:
                continue
            size = pad.GetSize()
            radius = max(pcbnew.ToMM(size.x), pcbnew.ToMM(size.y)) / 2.0
            if (
                point_segment_distance(mm_point(pad.GetPosition()), start, end)
                < radius + width / 2.0 + args.clearance
            ):
                for layer in pad_layers:
                    fixed_penalties[item_uuid][layer] += 5

    adjacency: dict[str, dict[str, int]] = defaultdict(dict)
    for (first, second), weight in pair_weights.items():
        adjacency[first][second] = adjacency[first].get(second, 0) + weight
        adjacency[second][first] = adjacency[second].get(first, 0) + weight

    layers = (
        COPPER_LAYERS
        if args.allow_inner
        else (pcbnew.F_Cu, pcbnew.B_Cu)
    )
    order = sorted(
        new_tracks,
        key=lambda item_uuid: (
            sum(adjacency[item_uuid].values()),
            sum(fixed_penalties[item_uuid].values()),
        ),
        reverse=True,
    )
    assignments: dict[str, int] = {}
    loads: Counter[int] = Counter()
    for item_uuid in order:
        choices = []
        for layer in layers:
            pair_cost = sum(
                weight
                for neighbor, weight in adjacency[item_uuid].items()
                if assignments.get(neighbor) == layer
            )
            fixed_cost = fixed_penalties[item_uuid][layer]
            choices.append(
                (pair_cost + fixed_cost + loads[layer] * 0.001, layer)
            )
        _, chosen = min(choices)
        assignments[item_uuid] = chosen
        loads[chosen] += 1

    changed = 0
    for item_uuid, layer in assignments.items():
        item = new_tracks[item_uuid]
        if item.GetLayer() != layer:
            item.SetLayer(layer)
            changed += 1

    pcbnew.ZONE_FILLER(candidate).Fill(candidate.Zones())
    args.output.parent.mkdir(parents=True, exist_ok=True)
    pcbnew.SaveBoard(str(args.output.resolve()), candidate)
    for suffix in (".kicad_pro", ".kicad_dru"):
        shutil.copyfile(
            hardware_dir / f"PocketLab-Card{suffix}", args.output.with_suffix(suffix)
        )
    candidate.BuildConnectivity()
    connectivity = candidate.GetConnectivity()
    connectivity.RecalculateRatsnest()
    print(
        "REASSIGNED "
        f"tracks={len(new_tracks)} immovable={len(candidate_added_tracks) - len(new_tracks)} "
        f"changed={changed} "
        f"opens={int(connectivity.GetUnconnectedCount(False))} "
        + " ".join(
            f"{candidate.GetLayerName(layer)}={loads[layer]}" for layer in layers
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
