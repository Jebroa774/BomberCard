"""Evaluate candidate-added copper one net at a time with KiCad DRC.

The aggressive routing candidate is useful as a connectivity reference, but a
large combined DRC report can obscure which route is independently safe.  This
tool transplants only the newly added copper for one net onto a clean base,
runs the real KiCad DRC, and records the resulting violation/open counts.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys

import pcbnew

from analyze_candidate_drc import geometry_key, uuid_text


COPPER_RE = re.compile(r"(?m)^\t\((segment|via)\b")
UUID_RE = re.compile(r'\(uuid\s+"?([0-9a-f-]{36})"?\)')


def expression_end(text: str, start: int) -> int:
    depth = 0
    quoted = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if quoted:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
            continue
        if char == '"':
            quoted = True
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return index + 1
    raise RuntimeError(f"unterminated expression at {start}")


def copper_blocks_by_uuid(text: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for match in COPPER_RE.finditer(text):
        start = match.start() + 1
        block = text[start : expression_end(text, start)]
        uuid_match = UUID_RE.search(block)
        if uuid_match:
            result[uuid_match.group(1)] = "\t" + block
    return result


def insert_blocks(base_text: str, blocks: list[str]) -> str:
    insertion = base_text.find("\n\t(zone")
    if insertion < 0:
        insertion = base_text.rfind("\n)")
    if insertion < 0:
        raise RuntimeError("could not locate PCB insertion point")
    return base_text[:insertion] + "\n" + "\n".join(blocks) + base_text[insertion:]


def counts(report_path: Path) -> tuple[int, int, dict[str, int]]:
    report = json.loads(report_path.read_text(encoding="utf-8"))
    violations = report.get("violations", [])
    return (
        len(violations),
        len(report.get("unconnected_items", [])),
        dict(Counter(item.get("type", "unknown") for item in violations)),
    )


def refill_zones(pcb_path: Path) -> None:
    """Rebuild zone fills after transplanted copper changes clearances."""
    board = pcbnew.LoadBoard(str(pcb_path))
    pcbnew.ZONE_FILLER(board).Fill(board.Zones())
    pcbnew.SaveBoard(str(pcb_path), board)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", type=Path, required=True)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--kicad-cli", type=Path, required=True)
    parser.add_argument("--work-prefix", type=Path, required=True)
    parser.add_argument("--summary", type=Path, required=True)
    parser.add_argument("--net", action="append", default=[])
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    base_path = args.base.resolve()
    source_path = args.source.resolve()
    prefix = args.work_prefix.resolve()
    pcb_path = prefix.with_suffix(".kicad_pcb")
    report_path = prefix.with_name(prefix.name + "-drc.json")
    if args.summary.exists() and not args.force:
        raise RuntimeError(f"summary exists: {args.summary}")

    base_board = pcbnew.LoadBoard(str(base_path))
    source_board = pcbnew.LoadBoard(str(source_path))
    base_keys = {geometry_key(item) for item in base_board.GetTracks()}
    new_items = [
        item for item in source_board.GetTracks() if geometry_key(item) not in base_keys
    ]
    new_uuids_by_net: dict[str, list[str]] = defaultdict(list)
    for item in new_items:
        new_uuids_by_net[item.GetNetname()].append(uuid_text(item))

    selected = set(args.net)
    nets = sorted(
        (net for net in new_uuids_by_net if not selected or net in selected),
        key=lambda net: (len(new_uuids_by_net[net]), net),
    )
    base_text = base_path.read_text(encoding="utf-8")
    source_blocks = copper_blocks_by_uuid(source_path.read_text(encoding="utf-8"))

    for suffix in (".kicad_pro", ".kicad_dru"):
        source_config = base_path.with_suffix(suffix)
        if not source_config.exists():
            source_config = base_path.parent / f"PocketLab-Card{suffix}"
        shutil.copyfile(source_config, prefix.with_suffix(suffix))

    # Establish authoritative base counts using the same temporary project.
    pcb_path.write_text(base_text, encoding="utf-8", newline="\n")
    refill_zones(pcb_path)
    subprocess.run(
        [
            str(args.kicad_cli.resolve()),
            "pcb",
            "drc",
            "--severity-all",
            "--format",
            "json",
            "--output",
            str(report_path),
            str(pcb_path),
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    base_violations, base_opens, base_types = counts(report_path)
    print(f"BASE drc={base_violations} open={base_opens}", flush=True)

    results: list[dict[str, object]] = []
    for index, net in enumerate(nets, 1):
        blocks = [source_blocks[item_uuid] for item_uuid in new_uuids_by_net[net]]
        pcb_path.write_text(insert_blocks(base_text, blocks), encoding="utf-8", newline="\n")
        refill_zones(pcb_path)
        subprocess.run(
            [
                str(args.kicad_cli.resolve()),
                "pcb",
                "drc",
                "--severity-all",
                "--format",
                "json",
                "--output",
                str(report_path),
                str(pcb_path),
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        violations, opens, types = counts(report_path)
        result = {
            "net": net,
            "new_items": len(blocks),
            "drc": violations,
            "drc_delta": violations - base_violations,
            "open": opens,
            "open_delta": opens - base_opens,
            "types": types,
        }
        results.append(result)
        print(
            f"[{index:02d}/{len(nets):02d}] {net:28s} items={len(blocks):3d} "
            f"drc={violations:4d} ({violations-base_violations:+4d}) "
            f"open={opens:3d} ({opens-base_opens:+3d})",
            flush=True,
        )
        args.summary.write_text(
            json.dumps(
                {
                    "base": {
                        "drc": base_violations,
                        "open": base_opens,
                        "types": base_types,
                    },
                    "results": results,
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
            newline="\n",
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
