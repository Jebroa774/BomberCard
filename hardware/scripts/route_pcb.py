"""Safely stage a PocketLab Card placement board through FreeRouting.

This script deliberately never writes the project's main PCB.  It applies a
small, deterministic set of KiCad netclasses in memory, exports Specctra DSN,
optionally imports a reviewed FreeRouting 2.3 SES into the
same in-memory board, validates the round trip, and only then publishes a
separate routed board.

Typical uses with KiCad's bundled Python::

    python route_pcb.py --export-only
    python route_pcb.py --import-existing-ses
    python route_pcb.py --jar C:\\path\\to\\freerouting-2.3.0.jar --java C:\\path\\to\\java.exe

The first command produces a DSN for routing in the FreeRouting GUI.  After
the GUI has written the requested SES, the second command imports it.  The
third command is the guarded headless path: it creates a second DSN whose
network section physically omits every manual/critical net before FreeRouting
is started.  This avoids relying on FreeRouting 2.3's broken headless ``-inc``
netclass exclusion.

Important engineering limit: autorouting is only a staging aid.  USB routing,
the provisional 50-ohm RF width, NFC matching/loop routing, switch nodes and
all power paths remain for manual routing.
The SES importer independently rejects any newly added track or via on these
manual nets.  Plane zones, return paths, impedance, antenna tuning and final
KiCad DRC are not made correct merely by a successful run of this script.
"""

from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
from typing import Iterable, Sequence


# pcbnew is loaded only after argument parsing so ``--help`` also works with a
# normal Python interpreter.  All real board work still requires KiCad 10's
# bundled Python, whose pcbnew bindings are not generally pip-installable.
pcbnew = None


@dataclass(frozen=True)
class NetClassSpec:
    name: str
    track_mm: float
    clearance_mm: float = 0.20
    via_mm: float = 0.60
    via_drill_mm: float = 0.30
    diff_width_mm: float = 0.20
    diff_gap_mm: float = 0.20
    diff_via_gap_mm: float = 0.20
    description: str = ""
    priority: int | None = None


DEFAULT_SPEC = NetClassSpec(
    "Default",
    0.20,
    diff_gap_mm=0.25,
    diff_via_gap_mm=0.25,
    description="PocketLab default; review every safety- or timing-critical route",
)
MANAGED_SPECS: tuple[NetClassSpec, ...] = (
    NetClassSpec(
        "POWER",
        0.60,
        clearance_mm=0.25,
        via_mm=0.80,
        via_drill_mm=0.40,
        description="Ordinary power fanout; planes and return paths still require review",
        priority=0,
    ),
    NetClassSpec(
        "USB_DIFF",
        0.20,
        diff_width_mm=0.20,
        diff_gap_mm=0.20,
        diff_via_gap_mm=0.25,
        description="Provisional USB differential geometry; route/review as a coupled pair",
        priority=1,
    ),
    NetClassSpec(
        "LF_RFID",
        0.40,
        clearance_mm=0.25,
        description="HTRC110 resonant/high-voltage and analogue paths; route manually and tune",
        priority=2,
    ),
    NetClassSpec(
        "SUBGHZ_RF",
        0.36,
        description="868-MHz 50-ohm feed and pi match; recalculate and tune on the assembled board",
        priority=3,
    ),
    NetClassSpec(
        "NFC_RF",
        0.40,
        clearance_mm=0.25,
        description="PN532 matching path; keep on one outer layer and tune the physical loop",
        priority=3,
    ),
    NetClassSpec(
        "SENSITIVE",
        0.20,
        description=(
            "Crystal, NFC receive and bias nodes; manually route short and via-free"
        ),
        priority=4,
    ),
)


# Assignments are intentionally exact net names, not broad wildcards.  Missing
# names are harmless and make the workflow tolerate small schematic revisions.
# A net may occur in only one group; validate_assignment_groups enforces that.
POWER_NETS = frozenset(
    {
        "+3V3",
        "+5V_AUX",
        "+5V_RAW",
        "LF_5V",
        "BAT_FET_MID",
        "CELL_NEG",
        "CELL_POS",
        "GND",
        "U6_L1",
        "U6_L2",
        "U7_SW",
        "VBUS_FUSED",
        "VBUS_USB",
        "VSYS",
    }
)
USB_NETS = frozenset(
    {
        "USB_CONN_N",
        "USB_CONN_P",
        "USB_D_N",
        "USB_D_P",
    }
)
USB_CONNECTOR_BRIDGE_NETS = frozenset({"USB_CONN_N", "USB_CONN_P"})
# J1 has duplicated F.Cu-only USB data pads.  The shortest clean breakout uses
# one tightly controlled B.Cu bridge per data net.  N also changes layer once
# beside U16 so it can enter the staggered bridge without crossing P.  Keep the
# five-via exception local instead of weakening the via-free MCU-side pair.
USB_CONNECTOR_EXPECTED_VIA_COUNTS = {"USB_CONN_N": 3, "USB_CONN_P": 2}
USB_CONNECTOR_VIA_REGION_MM = (92.20, 97.30, 48.00, 52.70)
USB_CONNECTOR_VIA_DIAMETER_MM = 0.50
USB_CONNECTOR_VIA_DRILL_MM = 0.30
USB_CONNECTOR_VIA_TOLERANCE_MM = 0.01
LF_RFID_NETS = frozenset(
    {
        "LF_ANT_A",
        "LF_ANT_B",
        "LF_CEXT",
        "LF_CLK_4M",
        "LF_QGND",
        "LF_RX",
        "LF_TAP",
        "LF_TX1",
        "LF_TX2",
    }
)
SUBGHZ_RF_NETS = frozenset({"SUBGHZ_RF_MOD", "SUBGHZ_RF_ANT"})
NFC_MATCH_NETS = frozenset(
    {"NFC_LOOP_A", "NFC_LOOP_B", "NFC_TX1", "NFC_TX1_F", "NFC_TX2", "NFC_TX2_F"}
)
SENSITIVE_NETS = frozenset(
    {
        "NFC_OSCIN",
        "NFC_OSCOUT",
        "RTC_OSCI",
        "RTC_OSCO",
        "NFC_RX_AC",
        "NFC_RX",
        "NFC_VMID",
    }
)
ASSIGNMENT_GROUPS: tuple[tuple[str, frozenset[str]], ...] = (
    ("POWER", POWER_NETS),
    ("USB_DIFF", USB_NETS),
    ("LF_RFID", LF_RFID_NETS),
    ("SUBGHZ_RF", SUBGHZ_RF_NETS),
    ("NFC_RF", NFC_MATCH_NETS),
    ("SENSITIVE", SENSITIVE_NETS),
)
# These classes are intentionally left for manual routing.  A guarded headless
# run removes their nets from a separate autorouter-only DSN instead of relying
# on FreeRouting 2.3's ineffective headless netclass exclusion.  The SES
# importer independently verifies that no new copper was added to them.
MANUAL_NETCLASSES = (
    "POWER", "USB_DIFF", "LF_RFID", "SUBGHZ_RF", "NFC_RF", "SENSITIVE"
)
MANUAL_LOGICAL_NETS = (
    POWER_NETS | USB_NETS | LF_RFID_NETS | SUBGHZ_RF_NETS | NFC_MATCH_NETS | SENSITIVE_NETS
)
DSN_POSITION_TOLERANCE_NM = 50
AUTOROUTER_CLEARANCE_UNITS = 250
AUTOROUTER_MIN_PATH_WIDTH_UNITS = 2000
REQUIRED_FINAL_NETS = USB_NETS | {
    "LF_ANT_A",
    "LF_ANT_B",
    "SUBGHZ_RF_MOD",
    "SUBGHZ_RF_ANT",
    "NFC_DVDD",
    "NFC_LOOP_A",
    "NFC_LOOP_B",
}
OBSOLETE_NETS = frozenset({"NFC_AVDD", "NFC_TVDD", "USB_DM_CONN", "USB_DP_CONN"})


def positive_int(value: str) -> int:
    result = int(value)
    if result < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return result


def nonnegative_int(value: str) -> int:
    result = int(value)
    if result < 0:
        raise argparse.ArgumentTypeError("must be zero or greater")
    return result


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    hardware_dir = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(
        description=(
            "Export a four-layer placement PCB to DSN and optionally route/import "
            "it without ever overwriting PocketLab-Card.kicad_pcb."
        ),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--input",
        type=Path,
        default=hardware_dir / "PocketLab-Card-planed.kicad_pcb",
        help="netlisted placement board with filled GND/+3V3 inner planes",
    )
    parser.add_argument(
        "--design",
        type=Path,
        default=hardware_dir / "design-netlist.json",
        help="generator JSON used to reject a stale placement-board netlist",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=hardware_dir / "PocketLab-Card-routed.kicad_pcb",
        help="separate routed KiCad board output",
    )
    parser.add_argument(
        "--dsn",
        type=Path,
        default=hardware_dir / "PocketLab-Card-routing.dsn",
        help="published Specctra design file",
    )
    parser.add_argument(
        "--autorouter-dsn",
        type=Path,
        default=hardware_dir / "PocketLab-Card-routing-digital.dsn",
        help=(
            "published audit copy used for guarded headless routing; critical "
            "nets are physically absent from its network section"
        ),
    )
    parser.add_argument(
        "--ses",
        type=Path,
        default=hardware_dir / "PocketLab-Card-routing.ses",
        help="published or externally generated Specctra session file",
    )
    parser.add_argument(
        "--java",
        default="java",
        help="Java executable used for a headless FreeRouting run",
    )
    parser.add_argument(
        "--jar",
        type=Path,
        help="FreeRouting 2.3.x JAR for a guarded digital-net-only headless run",
    )
    parser.add_argument(
        "--passes",
        type=positive_int,
        default=20,
        help="maximum FreeRouting autorouter passes (-mp)",
    )
    parser.add_argument(
        "--threads",
        type=nonnegative_int,
        default=max(1, min(8, (os.cpu_count() or 2) - 1)),
        help="FreeRouting optimizer threads (-mt)",
    )
    parser.add_argument(
        "--export-only",
        action="store_true",
        help="only publish a fresh DSN, even when --jar is supplied",
    )
    parser.add_argument(
        "--import-existing-ses",
        action="store_true",
        help="import an SES produced after the already-published DSN (no router run)",
    )
    parser.add_argument(
        "--allow-existing-tracks",
        action="store_true",
        help=(
            "explicitly accept pre-existing tracks; every original track must survive "
            "the SES import byte-for-geometry"
        ),
    )
    parser.add_argument(
        "--allow-existing-critical-vias",
        action="store_true",
        help=(
            "allow reviewed pre-existing vias on manually protected critical nets; "
            "the guarded SES import must still preserve every original copper item and "
            "may not add copper on those nets"
        ),
    )
    parser.add_argument(
        "--allow-stale-design-parity",
        action="store_true",
        help=(
            "route an explicitly selected historical PCB checkpoint even when its "
            "footprint set intentionally differs from design-netlist.json"
        ),
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="explicitly replace existing DSN/SES/output staging artifacts",
    )
    args = parser.parse_args(argv)
    if args.export_only and args.import_existing_ses:
        parser.error("--export-only and --import-existing-ses are mutually exclusive")
    if args.import_existing_ses and args.jar is not None:
        parser.error("--import-existing-ses cannot be combined with --jar")
    return args


def load_pcbnew() -> None:
    global pcbnew
    try:
        import pcbnew as pcbnew_module
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "pcbnew is unavailable. Run this script with KiCad 10's bundled Python."
        ) from exc
    if not str(pcbnew_module.GetBuildVersion()).startswith("10."):
        raise RuntimeError(
            f"KiCad 10 is required; loaded pcbnew {pcbnew_module.GetBuildVersion()}"
        )
    pcbnew = pcbnew_module


def canonical_key(path: Path) -> str:
    return os.path.normcase(str(path.expanduser().resolve(strict=False)))


def same_path(left: Path, right: Path) -> bool:
    if canonical_key(left) == canonical_key(right):
        return True
    if left.exists() and right.exists():
        try:
            return os.path.samefile(left, right)
        except OSError:
            pass
    return False


def validate_paths(args: argparse.Namespace, hardware_dir: Path) -> None:
    input_path = args.input.expanduser().resolve(strict=False)
    design_path = args.design.expanduser().resolve(strict=False)
    output_path = args.output.expanduser().resolve(strict=False)
    dsn_path = args.dsn.expanduser().resolve(strict=False)
    autorouter_dsn_path = args.autorouter_dsn.expanduser().resolve(strict=False)
    ses_path = args.ses.expanduser().resolve(strict=False)
    main_path = (hardware_dir / "PocketLab-Card.kicad_pcb").resolve(strict=False)

    if not input_path.is_file():
        raise RuntimeError(f"Input board does not exist: {input_path}")
    if not design_path.is_file():
        raise RuntimeError(f"Design netlist does not exist: {design_path}")
    if design_path.suffix.lower() != ".json":
        raise RuntimeError("--design must name a .json file")
    if input_path.suffix.lower() != ".kicad_pcb":
        raise RuntimeError("--input must name a .kicad_pcb file")
    if output_path.suffix.lower() != ".kicad_pcb":
        raise RuntimeError("--output must name a .kicad_pcb file")
    if dsn_path.suffix.lower() != ".dsn":
        raise RuntimeError("--dsn must name a .dsn file")
    if autorouter_dsn_path.suffix.lower() != ".dsn":
        raise RuntimeError("--autorouter-dsn must name a .dsn file")
    if ses_path.suffix.lower() != ".ses":
        raise RuntimeError("--ses must name a .ses file")
    if same_path(output_path, main_path):
        raise RuntimeError("Refusing to overwrite hardware/PocketLab-Card.kicad_pcb")
    if same_path(input_path, output_path):
        raise RuntimeError("--output must be separate from --input")

    named_paths = {
        "input": input_path,
        "design": design_path,
        "output": output_path,
        "dsn": dsn_path,
        "autorouter-dsn": autorouter_dsn_path,
        "ses": ses_path,
    }
    names = list(named_paths)
    for index, left_name in enumerate(names):
        for right_name in names[index + 1 :]:
            if same_path(named_paths[left_name], named_paths[right_name]):
                raise RuntimeError(f"--{left_name} and --{right_name} must be different files")


def require_destination_available(path: Path, force: bool) -> None:
    if path.exists() and not force:
        raise RuntimeError(f"Refusing to replace existing artifact without --force: {path}")
    if path.exists() and not path.is_file():
        raise RuntimeError(f"Destination exists but is not a regular file: {path}")


def project_companions(
    hardware_dir: Path, output_path: Path
) -> tuple[tuple[Path, Path], ...]:
    """Return main-project sources and basename-matched output companions."""
    pairs = tuple(
        (
            hardware_dir / f"PocketLab-Card{suffix}",
            output_path.with_suffix(suffix),
        )
        for suffix in (".kicad_pro", ".kicad_dru")
    )
    for source, destination in pairs:
        if not source.is_file():
            raise RuntimeError(f"Required main-project companion is missing: {source}")
        if same_path(source, destination):
            raise RuntimeError(
                f"Refusing to replace the main-project companion: {destination}"
            )
    return pairs


def publish_file(source: Path, destination: Path, force: bool) -> None:
    """Publish a validated temporary file without an implicit overwrite."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        shutil.copy2(source, temporary)
        if force:
            os.replace(temporary, destination)
        else:
            # A hard link publishes the already-complete temporary inode in one
            # operation and fails rather than replacing an existing path.
            try:
                os.link(temporary, destination)
            except FileExistsError as exc:
                raise RuntimeError(
                    f"Refusing to replace existing artifact without --force: {destination}"
                ) from exc
    finally:
        if temporary.exists():
            temporary.unlink()


def publish_board_bundle(
    candidate_board: Path,
    output_path: Path,
    companion_pairs: tuple[tuple[Path, Path], ...],
    force: bool,
) -> None:
    """Publish the PCB and its basename-matched project/rule companions.

    Availability is checked for the complete bundle before the first atomic
    file publication.  This prevents an already-present companion from being
    discovered only after the routed PCB has been written.
    """
    destinations = (output_path,) + tuple(
        destination for _source, destination in companion_pairs
    )
    for destination in destinations:
        require_destination_available(destination, force)

    # Publish companions first so a newly visible PCB never lacks the project
    # and custom-rule files that KiCad discovers by matching its basename.
    for source, destination in companion_pairs:
        publish_file(source, destination, force)
    publish_file(candidate_board, output_path, force)


def board_net_names(board) -> frozenset[str]:
    return frozenset(str(name) for name in board.GetNetsByName().keys() if str(name))


def load_design_parts(path: Path) -> list[dict[str, object]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Cannot read design netlist {path}: {exc}") from exc
    if payload.get("format") != 1 or not isinstance(payload.get("parts"), list):
        raise RuntimeError(f"Unsupported design-netlist format: {path}")
    parts = payload["parts"]
    references = [str(part.get("reference", "")) for part in parts]
    duplicates = sorted({reference for reference in references if references.count(reference) > 1})
    if "" in references or duplicates:
        raise RuntimeError(
            "Invalid design references; blanks="
            + str("" in references)
            + ", duplicates="
            + repr(duplicates)
        )
    return parts


def field_is_true(value: object) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes"}


def root_local_net_name(logical_name: object) -> str:
    """Return KiCad's physical name for a local label on the root sheet."""
    name = str(logical_name)
    if not name or name.startswith("/"):
        raise RuntimeError(f"Expected an unscoped logical net name, got {name!r}")
    return f"/{name}"


def pin_board_net_name(
    part: dict[str, object], pin_number: object, logical_name: object
) -> str:
    if logical_name is not None and str(logical_name):
        return root_local_net_name(logical_name)
    pin = str(pin_number)
    no_connect_nets = {
        str(number): str(name)
        for number, name in dict(part.get("no_connect_nets", {})).items()
    }
    try:
        return no_connect_nets[pin]
    except KeyError as exc:
        raise RuntimeError(
            f"{part.get('reference', '?')}.{pin}: missing generated no-connect net name"
        ) from exc


def validate_design_parity(board, design_path: Path) -> None:
    """Reject an old placement board even when its gross counts look plausible."""
    parts = load_design_parts(design_path)
    populated = [part for part in parts if str(part.get("footprint", ""))]
    expected_by_ref = {str(part["reference"]): part for part in populated}
    actual_by_ref = {footprint.GetReference(): footprint for footprint in board.GetFootprints()}
    if expected_by_ref.keys() != actual_by_ref.keys():
        raise RuntimeError(
            "Placement board is stale relative to design-netlist.json; footprint refs differ. "
            "Re-run build_pcb.py."
        )

    expected_logical_nets = frozenset(
        str(net_name)
        for part in populated
        for net_name in dict(part.get("pins", {})).values()
        if net_name is not None and str(net_name)
    )
    missing_final = REQUIRED_FINAL_NETS - expected_logical_nets
    obsolete = OBSOLETE_NETS & expected_logical_nets
    if missing_final or obsolete:
        raise RuntimeError(
            "design-netlist.json is not the final routing revision; missing="
            + repr(sorted(missing_final))
            + ", obsolete="
            + repr(sorted(obsolete))
        )
    expected_nets = frozenset(
        pin_board_net_name(part, pin, net_name)
        for part in populated
        for pin, net_name in dict(part.get("pins", {})).items()
    )
    actual_nets = board_net_names(board)
    if actual_nets != expected_nets:
        raise RuntimeError(
            "Placement board netlist is stale; re-run build_pcb.py. Missing="
            + repr(sorted(expected_nets - actual_nets))
            + ", obsolete/extra="
            + repr(sorted(actual_nets - expected_nets))
        )

    mismatches: list[str] = []
    for reference, part in expected_by_ref.items():
        footprint = actual_by_ref[reference]
        actual_id = footprint.GetFPID().GetUniStringLibId()
        expected_id = str(part["footprint"])
        fields = {str(name): str(value) for name, value in dict(part.get("fields", {})).items()}
        expected_dnp = bool(
            part.get("dnp", field_is_true(fields.get("DNP", "")))
        )
        expected_in_bom = bool(part.get("in_bom", True))
        if actual_id != expected_id:
            mismatches.append(f"{reference}:footprint {actual_id!r}!={expected_id!r}")
        if footprint.GetValue() != str(part.get("value", "")):
            mismatches.append(f"{reference}:value")
        if bool(footprint.IsDNP()) != expected_dnp:
            mismatches.append(f"{reference}:DNP")
        if bool(footprint.IsExcludedFromBOM()) == expected_in_bom:
            mismatches.append(f"{reference}:in_bom")
        for field_name, field_value in fields.items():
            if not footprint.HasField(field_name):
                mismatches.append(f"{reference}:missing field {field_name}")
            elif footprint.GetFieldText(field_name) != field_value:
                mismatches.append(f"{reference}:field {field_name}")

        actual_pad_nets: dict[str, set[str]] = {}
        for pad in footprint.Pads():
            if pad.GetNumber():
                actual_pad_nets.setdefault(pad.GetNumber(), set()).add(pad.GetNetname())
        for pin, net_name_value in dict(part.get("pins", {})).items():
            net_name = pin_board_net_name(part, pin, net_name_value)
            if net_name not in actual_pad_nets.get(str(pin), set()):
                mismatches.append(f"{reference}.{pin}:{net_name}")
    if mismatches:
        raise RuntimeError(
            "Placement board differs from the generated values/footprints/pad nets; "
            "re-run build_pcb.py. First mismatches: "
            + ", ".join(mismatches[:12])
        )


def pad_signature(footprint) -> tuple[tuple[str, str], ...]:
    return tuple(
        sorted((pad.GetNumber(), pad.GetNetname()) for pad in footprint.Pads())
    )


def footprint_signature(footprint) -> tuple[object, ...]:
    position = footprint.GetPosition()
    footprint_id = footprint.GetFPID().GetUniStringLibId()
    return (
        footprint_id,
        int(footprint.GetLayer()),
        int(position.x),
        int(position.y),
        round(float(footprint.GetOrientationDegrees()), 6),
        bool(footprint.IsDNP()),
        bool(footprint.IsExcludedFromBOM()),
        tuple(sorted(footprint.GetFieldsText().items())),
        pad_signature(footprint),
    )


def footprint_snapshot(board) -> dict[str, tuple[object, ...]]:
    result: dict[str, tuple[object, ...]] = {}
    for footprint in board.GetFootprints():
        reference = footprint.GetReference()
        if not reference:
            raise RuntimeError("Every footprint must have a reference")
        if reference in result:
            raise RuntimeError(f"Duplicate footprint reference: {reference}")
        result[reference] = footprint_signature(footprint)
    return result


def footprint_signatures_equivalent(
    before: tuple[object, ...], after: tuple[object, ...]
) -> bool:
    """Accept only the unavoidable 100 nm Specctra position quantization."""
    if len(before) != len(after):
        return False
    if before[0:2] != after[0:2] or before[4:] != after[4:]:
        return False
    return all(
        abs(int(before[index]) - int(after[index])) <= DSN_POSITION_TOLERANCE_NM
        for index in (2, 3)
    )


FOOTPRINT_SIGNATURE_FIELDS = (
    "library_id",
    "layer",
    "x_nm",
    "y_nm",
    "orientation_deg",
    "dnp",
    "excluded_from_bom",
    "fields",
    "pads",
)


def describe_footprint_signature_change(
    reference: str,
    before: tuple[object, ...],
    after: tuple[object, ...],
) -> str:
    changes: list[str] = []
    for name, original, imported in zip(
        FOOTPRINT_SIGNATURE_FIELDS, before, after, strict=True
    ):
        if original == imported:
            continue
        if name in {"fields", "pads"}:
            changes.append(name)
        else:
            changes.append(f"{name} {original!r}->{imported!r}")
    return f"{reference}({'; '.join(changes)})"


def point_tuple(point) -> tuple[int, int]:
    return int(point.x), int(point.y)


def layer_tuple(item) -> tuple[int, ...]:
    return tuple(int(layer) for layer in item.GetLayerSet().Seq())


def track_signature(item) -> tuple[object, ...]:
    if isinstance(item, pcbnew.PCB_VIA):
        return (
            "via",
            item.GetNetname(),
            point_tuple(item.GetPosition()),
            # KiCad 10 asserts when PCB_VIA.GetWidth() is called without the
            # layer overload, even for an ordinary through via.
            int(item.GetWidth(pcbnew.F_Cu)),
            int(item.GetDrillValue()),
            int(item.GetViaType()),
            layer_tuple(item),
        )
    if isinstance(item, pcbnew.PCB_ARC):
        return (
            "arc",
            item.GetNetname(),
            point_tuple(item.GetStart()),
            point_tuple(item.GetMid()),
            point_tuple(item.GetEnd()),
            int(item.GetWidth()),
            int(item.GetLayer()),
        )
    return (
        "segment",
        item.GetNetname(),
        point_tuple(item.GetStart()),
        point_tuple(item.GetEnd()),
        int(item.GetWidth()),
        int(item.GetLayer()),
    )


def track_snapshot(board) -> Counter[tuple[object, ...]]:
    return Counter(track_signature(track) for track in board.GetTracks())


def zone_snapshot(board) -> Counter[tuple[object, ...]]:
    result: Counter[tuple[object, ...]] = Counter()
    for zone in board.Zones():
        result[(zone.GetNetname(), layer_tuple(zone), zone.GetZoneName())] += 1
    return result


def unconnected_edge_count(board) -> int:
    board.BuildConnectivity()
    connectivity = board.GetConnectivity()
    connectivity.RecalculateRatsnest()
    return int(connectivity.GetUnconnectedCount(False))


def validate_four_layer_plane_stack(board) -> tuple[str, str, str, str]:
    if board.GetCopperLayerCount() != 4:
        raise RuntimeError(
            f"Exactly four copper layers are required; board has {board.GetCopperLayerCount()}"
        )
    layers = (pcbnew.F_Cu, pcbnew.In1_Cu, pcbnew.In2_Cu, pcbnew.B_Cu)
    if not all(board.IsLayerEnabled(layer) for layer in layers):
        raise RuntimeError("F.Cu, In1.Cu, In2.Cu and B.Cu must all be enabled")
    if board.GetLayerType(pcbnew.F_Cu) != pcbnew.LT_SIGNAL:
        raise RuntimeError("F.Cu must be a signal layer")
    if board.GetLayerType(pcbnew.B_Cu) != pcbnew.LT_SIGNAL:
        raise RuntimeError("B.Cu must be a signal layer")
    for layer in (pcbnew.In1_Cu, pcbnew.In2_Cu):
        if board.GetLayerType(layer) != pcbnew.LT_POWER:
            raise RuntimeError(
                f"Inner layer {board.GetLayerName(layer)} must be type 'power', not routable signal"
            )
    return tuple(board.GetLayerName(layer) for layer in layers)


def validate_required_planes(board) -> None:
    required = {
        (pcbnew.In1_Cu, "/GND"),
        (pcbnew.In2_Cu, "/+3V3"),
    }
    found: set[tuple[int, str]] = set()
    for zone in board.Zones():
        for layer, net_name in required:
            if (
                zone.GetNetname() == net_name
                and zone.IsOnLayer(layer)
                and zone.IsFilled()
                and zone.HasFilledPolysForLayer(layer)
            ):
                found.add((layer, net_name))
    missing = required - found
    if missing:
        descriptions = [
            f"{board.GetLayerName(layer)}={net_name}" for layer, net_name in sorted(missing)
        ]
        raise RuntimeError(
            "Routing input must come from add_planes.py and contain filled inner planes; "
            "missing "
            + ", ".join(descriptions)
        )


def validate_no_inner_tracks(board) -> None:
    inner_layers = {pcbnew.In1_Cu, pcbnew.In2_Cu}
    offenders: list[str] = []
    for item in board.GetTracks():
        if isinstance(item, pcbnew.PCB_VIA):
            # Through vias may cross plane layers; only planar copper segments
            # are prohibited on the two inner layers.
            continue
        if item.GetLayer() in inner_layers:
            offenders.append(f"{item.GetNetname()}:{board.GetLayerName(item.GetLayer())}")
    if offenders:
        raise RuntimeError(
            "Inner layers are reserved for planes, but tracks were found there: "
            + ", ".join(offenders[:12])
        )


def validate_no_new_inner_tracks(
    original_tracks: Counter[tuple[object, ...]],
    actual_tracks: Counter[tuple[object, ...]],
) -> None:
    """Keep the reviewed L3 corridors, but reject new autorouter inner copper.

    Later manual routing stages intentionally use a small number of In2.Cu
    corridors through the +3V3 plane.  They are part of the protected input
    geometry and must survive unchanged.  FreeRouting must still add tracks
    only on the two outer signal layers.
    """
    added_tracks = actual_tracks - original_tracks
    offenders: list[str] = []
    for signature, count in added_tracks.items():
        kind = str(signature[0])
        if kind == "via":
            continue
        layer = int(signature[-1])
        if layer in {int(pcbnew.In1_Cu), int(pcbnew.In2_Cu)}:
            offenders.append(f"{signature[1]}:{kind} x{count}")
    if offenders:
        raise RuntimeError(
            "Autorouter added copper on the protected inner plane layers: "
            + ", ".join(offenders[:12])
        )


def validate_no_critical_vias(board) -> None:
    # Mirrors the project's .kicad_dru safety rules.  FreeRouting's DSN input
    # does not reliably express a per-net no-via constraint, so reject a route
    # that violates it instead of silently accepting the autorouter result.
    no_via_nets = {
        root_local_net_name(name)
        for name in (
            (USB_NETS - USB_CONNECTOR_BRIDGE_NETS)
            | NFC_MATCH_NETS
            | SENSITIVE_NETS
            | LF_RFID_NETS
        )
    }
    offenders = sorted(
        {
            item.GetNetname()
            for item in board.GetTracks()
            if isinstance(item, pcbnew.PCB_VIA) and item.GetNetname() in no_via_nets
        }
    )
    if offenders:
        raise RuntimeError(
            "Critical nets must remain via-free according to PocketLab-Card.kicad_dru: "
            + ", ".join(offenders)
        )

    connector_nets = {
        root_local_net_name(name): name for name in USB_CONNECTOR_BRIDGE_NETS
    }
    bridge_vias: dict[str, list[object]] = {
        physical_name: [] for physical_name in connector_nets
    }
    for item in board.GetTracks():
        if isinstance(item, pcbnew.PCB_VIA) and item.GetNetname() in bridge_vias:
            bridge_vias[item.GetNetname()].append(item)

    # A placement board has no USB bridge yet and must remain a valid autorouter
    # input.  Once manual USB routing starts, require both complete bridges so a
    # partial or accidentally added exception can never pass publication checks.
    via_count = sum(len(vias) for vias in bridge_vias.values())
    if via_count == 0:
        return
    expected_counts = {
        root_local_net_name(name): count
        for name, count in USB_CONNECTOR_EXPECTED_VIA_COUNTS.items()
    }
    invalid_counts = {
        connector_nets[net_name]: (len(vias), expected_counts[net_name])
        for net_name, vias in bridge_vias.items()
        if len(vias) != expected_counts[net_name]
    }
    if invalid_counts:
        details = ", ".join(
            f"{name}={actual} (expected {expected})"
            for name, (actual, expected) in sorted(invalid_counts.items())
        )
        raise RuntimeError(
            "The reviewed J1 USB breakout via count changed; " f"found {details}"
        )

    x_min, x_max, y_min, y_max = USB_CONNECTOR_VIA_REGION_MM
    geometry_offenders: list[str] = []
    for net_name, vias in bridge_vias.items():
        for via in vias:
            position = via.GetPosition()
            x_mm = pcbnew.ToMM(position.x)
            y_mm = pcbnew.ToMM(position.y)
            diameter_mm = pcbnew.ToMM(via.GetWidth(pcbnew.F_Cu))
            drill_mm = pcbnew.ToMM(via.GetDrillValue())
            if via.GetViaType() != pcbnew.VIATYPE_THROUGH:
                geometry_offenders.append(
                    f"{connector_nets[net_name]}@({x_mm:.2f},{y_mm:.2f}) is not through"
                )
            elif not (x_min <= x_mm <= x_max and y_min <= y_mm <= y_max):
                geometry_offenders.append(
                    f"{connector_nets[net_name]}@({x_mm:.2f},{y_mm:.2f}) outside J1"
                )
            elif (
                abs(diameter_mm - USB_CONNECTOR_VIA_DIAMETER_MM)
                > USB_CONNECTOR_VIA_TOLERANCE_MM
                or abs(drill_mm - USB_CONNECTOR_VIA_DRILL_MM)
                > USB_CONNECTOR_VIA_TOLERANCE_MM
            ):
                geometry_offenders.append(
                    f"{connector_nets[net_name]}@({x_mm:.2f},{y_mm:.2f}) "
                    f"is {diameter_mm:.2f}/{drill_mm:.2f} mm"
                )
    if geometry_offenders:
        raise RuntimeError(
            "J1 USB breakout vias must be 0.50/0.30 mm through vias inside the "
            "connector breakout region: "
            + "; ".join(geometry_offenders)
        )


def validate_assignment_groups() -> None:
    seen: dict[str, str] = {}
    for class_name, names in ASSIGNMENT_GROUPS:
        for name in names:
            previous = seen.setdefault(name, class_name)
            if previous != class_name:
                raise RuntimeError(
                    f"Internal error: net {name} occurs in {previous} and {class_name}"
                )


def set_class_dimensions(netclass, spec: NetClassSpec) -> None:
    netclass.SetClearance(pcbnew.FromMM(spec.clearance_mm))
    netclass.SetTrackWidth(pcbnew.FromMM(spec.track_mm))
    netclass.SetViaDiameter(pcbnew.FromMM(spec.via_mm))
    netclass.SetViaDrill(pcbnew.FromMM(spec.via_drill_mm))
    netclass.SetDiffPairWidth(pcbnew.FromMM(spec.diff_width_mm))
    netclass.SetDiffPairGap(pcbnew.FromMM(spec.diff_gap_mm))
    netclass.SetDiffPairViaGap(pcbnew.FromMM(spec.diff_via_gap_mm))
    netclass.SetDescription(spec.description)
    if spec.priority is not None:
        netclass.SetPriority(spec.priority)


def configure_netclasses(board) -> dict[str, NetClassSpec]:
    validate_assignment_groups()
    settings = board.GetDesignSettings().m_NetSettings
    default_class = settings.GetDefaultNetclass()
    set_class_dimensions(default_class, DEFAULT_SPEC)
    settings.SetDefaultNetclass(default_class)

    spec_by_name = {spec.name: spec for spec in MANAGED_SPECS}
    for spec in MANAGED_SPECS:
        netclass = pcbnew.NETCLASS(spec.name)
        set_class_dimensions(netclass, spec)
        settings.SetNetclass(spec.name, netclass)

    assignments: dict[str, NetClassSpec] = {}
    actual_nets = board_net_names(board)
    for class_name, names in ASSIGNMENT_GROUPS:
        for logical_name in sorted(names):
            net_name = root_local_net_name(logical_name)
            if net_name not in actual_nets:
                continue
            class_set = pcbnew.STRINGSET()
            class_set.insert(class_name)
            # This is an exact net-name assignment.  Unlike a wildcard pattern,
            # it does not unexpectedly capture future schematic labels.
            settings.SetNetclassLabelAssignment(net_name, class_set)
            assignments[net_name] = spec_by_name[class_name]

    settings.ClearAllCaches()
    settings.RecomputeEffectiveNetclasses()
    board.SynchronizeNetsAndNetClasses(False)
    validate_netclasses(board, assignments)
    return assignments


def validate_netclasses(board, assignments: dict[str, NetClassSpec]) -> None:
    settings = board.GetDesignSettings().m_NetSettings
    default_class = settings.GetDefaultNetclass()
    if default_class.GetTrackWidth() != pcbnew.FromMM(DEFAULT_SPEC.track_mm):
        raise RuntimeError("Default netclass did not retain its 0.20 mm width")
    if default_class.GetClearance() != pcbnew.FromMM(DEFAULT_SPEC.clearance_mm):
        raise RuntimeError("Default netclass did not retain its 0.20 mm clearance")

    for net_name, spec in assignments.items():
        effective = settings.GetEffectiveNetClass(net_name)
        expected = (
            pcbnew.FromMM(spec.track_mm),
            pcbnew.FromMM(spec.clearance_mm),
            pcbnew.FromMM(spec.diff_width_mm),
            pcbnew.FromMM(spec.diff_gap_mm),
        )
        actual = (
            effective.GetTrackWidth(),
            effective.GetClearance(),
            effective.GetDiffPairWidth(),
            effective.GetDiffPairGap(),
        )
        if actual != expected:
            raise RuntimeError(
                f"Netclass conflict for {net_name}: expected {expected}, got {actual} "
                f"from {effective.GetHumanReadableName()}"
            )


DSN_LAYER_RE = re.compile(
    r"\(layer\s+(?:\"([^\"]+)\"|([^\s()]+))\s+\(type\s+([^\s()]+)\)",
    re.MULTILINE,
)
DSN_IDENTITY_RE = re.compile(r'\A\(pcb\s+"[^"]*"')


def normalize_dsn_identity(path: Path, published_name: str) -> None:
    """Remove the random temporary export path from the DSN design identity."""
    if '"' in published_name or "\n" in published_name or "\r" in published_name:
        raise RuntimeError(f"Unsafe DSN filename: {published_name!r}")
    text = path.read_text(encoding="utf-8", errors="strict")
    normalized, replacements = DSN_IDENTITY_RE.subn(
        f'(pcb "{published_name}"', text, count=1
    )
    if replacements != 1:
        raise RuntimeError("Could not normalize the KiCad DSN design identity")
    path.write_text(normalized, encoding="utf-8", newline="\n")


def validate_exported_dsn(path: Path, layer_names: tuple[str, str, str, str]) -> None:
    if not path.is_file() or path.stat().st_size < 100:
        raise RuntimeError(f"KiCad did not create a usable DSN: {path}")
    text = path.read_text(encoding="utf-8", errors="strict")
    discovered: dict[str, str] = {}
    for quoted_name, bare_name, layer_type in DSN_LAYER_RE.findall(text):
        discovered[quoted_name or bare_name] = layer_type
    expected = {
        layer_names[0]: "signal",
        layer_names[1]: "power",
        layer_names[2]: "power",
        layer_names[3]: "signal",
    }
    if discovered != expected:
        raise RuntimeError(
            f"Unexpected DSN layer declarations: expected {expected}, got {discovered}"
        )


def dsn_atom(text: str, offset: int) -> tuple[str, int]:
    """Read one bare or quoted Specctra atom without interpreting its value."""
    length = len(text)
    while offset < length and text[offset].isspace():
        offset += 1
    if offset >= length:
        raise RuntimeError("Unexpected end of DSN while reading an atom")
    if text[offset] == '"':
        offset += 1
        value: list[str] = []
        escaped = False
        while offset < length:
            character = text[offset]
            offset += 1
            if escaped:
                value.append(character)
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                return "".join(value), offset
            else:
                value.append(character)
        raise RuntimeError("Unterminated quoted atom in DSN")
    start = offset
    while offset < length and not text[offset].isspace() and text[offset] not in "()":
        offset += 1
    if start == offset:
        raise RuntimeError(f"Expected DSN atom at offset {offset}")
    return text[start:offset], offset


def dsn_expression_end(text: str, start: int) -> int:
    """Return the exclusive end offset of the balanced expression at start."""
    if start >= len(text) or text[start] != "(":
        raise RuntimeError(f"Expected DSN expression at offset {start}")
    depth = 0
    quoted = False
    escaped = False
    for offset in range(start, len(text)):
        character = text[offset]
        if quoted:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                quoted = False
            continue
        if character == '"':
            quoted = True
        elif character == "(":
            depth += 1
        elif character == ")":
            depth -= 1
            if depth == 0:
                return offset + 1
            if depth < 0:
                break
    raise RuntimeError(f"Unbalanced DSN expression at offset {start}")


def filter_autorouter_network(source: Path, destination: Path) -> tuple[int, int]:
    """Create a DSN whose manual nets have no routable pin connections.

    FreeRouting 2.3's headless path ignores class exclusions.  Removing the
    protected pin lists from an autorouter-only copy makes those nets
    unroutable by construction.  Their empty net declarations are retained so
    FreeRouting can still load the existing fixed wires and vias as obstacles;
    deleting the declarations produces thousands of ``net not found`` warnings
    and can stall the headless router.  The full DSN remains the publication
    source, and the SES importer still rejects protected-net copper
    independently.
    """
    text = source.read_text(encoding="utf-8", errors="strict")
    matches = list(re.finditer(r"(?m)^\s*\(network\b", text))
    if len(matches) != 1:
        raise RuntimeError(f"Expected exactly one DSN network section, found {len(matches)}")
    network_start = text.find("(", matches[0].start())
    network_end = dsn_expression_end(text, network_start)
    head, cursor = dsn_atom(text, network_start + 1)
    if head != "network":
        raise RuntimeError(f"Expected DSN network expression, got {head!r}")

    manual_physical_nets = frozenset(root_local_net_name(name) for name in MANUAL_LOGICAL_NETS)
    seen_manual_nets: set[str] = set()
    retained_nets = 0
    pieces = [text[:cursor]]
    copy_from = cursor
    while cursor < network_end - 1:
        while cursor < network_end - 1 and text[cursor].isspace():
            cursor += 1
        if cursor >= network_end - 1:
            break
        if text[cursor] != "(":
            raise RuntimeError(f"Unexpected token in DSN network section at offset {cursor}")
        child_start = cursor
        child_end = dsn_expression_end(text, child_start)
        child_head, atom_offset = dsn_atom(text, child_start + 1)
        child_name, _ = dsn_atom(text, atom_offset)
        remove = False
        replacement: str | None = None
        if child_head == "net":
            if child_name in manual_physical_nets:
                seen_manual_nets.add(child_name)
                remove = True
                replacement = f"(net {child_name})"
            else:
                retained_nets += 1
        elif child_head == "class" and child_name in MANUAL_NETCLASSES:
            remove = True

        if remove:
            pieces.append(text[copy_from:child_start])
            if replacement is not None:
                pieces.append(replacement)
            copy_from = child_end
        cursor = child_end

    pieces.append(text[copy_from:])
    filtered = "".join(pieces)
    expected_manual_nets = manual_physical_nets & {
        root_local_net_name(name)
        for name in MANUAL_LOGICAL_NETS
        if re.search(
            r"\(net\s+" + re.escape(root_local_net_name(name)) + r"(?=\s|\))",
            text,
        )
    }
    if seen_manual_nets != expected_manual_nets:
        raise RuntimeError(
            "Autorouter DSN filter did not remove the exact protected-net set; missing="
            + repr(sorted(expected_manual_nets - seen_manual_nets))
            + ", unexpected="
            + repr(sorted(seen_manual_nets - expected_manual_nets))
        )
    if retained_nets < 1:
        raise RuntimeError("Autorouter DSN filter left no routable nets")
    filtered_network_match = re.search(r"(?m)^\s*\(network\b", filtered)
    if filtered_network_match is None:
        raise RuntimeError("Filtered autorouter DSN lost its network section")
    filtered_network_start = filtered.find("(", filtered_network_match.start())
    filtered_network_end = dsn_expression_end(filtered, filtered_network_start)
    filtered_network = filtered[filtered_network_start:filtered_network_end]
    for net_name in seen_manual_nets:
        match = re.search(
            r"\(net\s+" + re.escape(net_name) + r"(?=\s|\))",
            filtered_network,
        )
        if match is None:
            raise RuntimeError(f"Protected net declaration was lost: {net_name}")
        net_end = dsn_expression_end(filtered_network, match.start())
        if "(pins" in filtered_network[match.start():net_end]:
            raise RuntimeError(f"Protected net still has routable pins: {net_name}")
    if "(resolution um 10)" not in filtered:
        raise RuntimeError("Guarded autorouter requires the expected 10-unit-per-um DSN resolution")
    filtered, clearance_replacements = re.subn(
        r"\(clearance 200\)",
        f"(clearance {AUTOROUTER_CLEARANCE_UNITS})",
        filtered,
    )
    if clearance_replacements < 2:
        raise RuntimeError(
            "Expected to strengthen at least the global and default-class DSN clearances; "
            f"changed {clearance_replacements}"
        )
    destination.write_text(filtered, encoding="utf-8", newline="\n")
    return len(seen_manual_nets), retained_nets


SES_PATH_WIDTH_RE = re.compile(r"(\(path\s+(?:F\.Cu|B\.Cu)\s+)(\d+)")


def normalize_freerouting_session(path: Path) -> int:
    """Reject inner routing and promote FreeRouting pin-neckdowns to 0.20 mm."""
    text = path.read_text(encoding="utf-8", errors="strict")
    if "(resolution um 10)" not in text:
        raise RuntimeError("FreeRouting SES does not use the expected 10-unit-per-um resolution")
    if re.search(r"\(path\s+(?:GND|PWR)\s+", text):
        raise RuntimeError("FreeRouting SES contains a forbidden path on L2/L3")
    promoted = 0

    def promote(match: re.Match[str]) -> str:
        nonlocal promoted
        width = int(match.group(2))
        if width >= AUTOROUTER_MIN_PATH_WIDTH_UNITS:
            return match.group(0)
        promoted += 1
        return match.group(1) + str(AUTOROUTER_MIN_PATH_WIDTH_UNITS)

    normalized = SES_PATH_WIDTH_RE.sub(promote, text)
    path.write_text(normalized, encoding="utf-8", newline="\n")
    return promoted


def resolve_java(java_argument: str) -> str:
    candidate = Path(java_argument).expanduser()
    if candidate.is_file():
        return str(candidate.resolve())
    discovered = shutil.which(java_argument)
    if discovered:
        return discovered
    raise RuntimeError(f"Java executable not found: {java_argument}")


def validate_freerouting_jar(java: str, jar: Path) -> None:
    if not jar.is_file() or jar.suffix.lower() != ".jar":
        raise RuntimeError(f"FreeRouting JAR does not exist or is not a .jar: {jar}")
    probe = subprocess.run(
        [java, "-jar", str(jar), "-h"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        errors="replace",
        timeout=30,
        check=False,
    )
    match = re.search(r"Freerouting\s+v(\d+)\.(\d+)\.(\d+)", probe.stdout, re.I)
    # FreeRouting 2.3.0 deliberately exits with status 1 after printing CLI
    # help, even though the JAR and version probe are valid.  Treat the parsed
    # official version banner as the acceptance signal instead of relying on
    # that non-portable help exit code.
    if not match:
        raise RuntimeError(
            "Could not verify the FreeRouting version from `java -jar ... -h`:\n"
            + probe.stdout[-2000:]
        )
    if (int(match.group(1)), int(match.group(2))) != (2, 3):
        raise RuntimeError(
            f"FreeRouting 2.3.x is required; JAR reports {match.group(0)}"
        )


def run_freerouting(
    java: str,
    jar: Path,
    dsn: Path,
    ses: Path,
    passes: int,
    threads: int,
) -> None:
    command = (
        java,
        "-jar",
        str(jar),
        "--gui.enabled=false",
        "-de",
        str(dsn),
        "-do",
        str(ses),
        "-mp",
        str(passes),
        "-mt",
        str(threads),
        "-l",
        "en",
    )
    print(
        f"Starting guarded FreeRouting run: {passes} passes, {threads} threads, "
        f"input={dsn.name}"
    )
    completed = subprocess.run(command, check=False)
    if completed.returncode != 0:
        raise RuntimeError(f"FreeRouting failed with exit code {completed.returncode}")
    if not ses.is_file() or ses.stat().st_size < 100:
        raise RuntimeError(f"FreeRouting did not create a usable SES: {ses}")


def validate_round_trip(
    board,
    original_footprints: dict[str, tuple[object, ...]],
    original_nets: frozenset[str],
    original_tracks: Counter[tuple[object, ...]],
    original_zones: Counter[tuple[object, ...]],
    assignments: dict[str, NetClassSpec],
    allow_existing_critical_vias: bool,
) -> None:
    actual_footprints = footprint_snapshot(board)
    if actual_footprints.keys() != original_footprints.keys():
        raise RuntimeError(
            "Footprint round-trip mismatch; missing="
            + repr(sorted(original_footprints.keys() - actual_footprints.keys()))
            + ", extra="
            + repr(sorted(actual_footprints.keys() - original_footprints.keys()))
        )
    changed_footprints = [
        reference
        for reference in original_footprints
        if not footprint_signatures_equivalent(
            original_footprints[reference], actual_footprints[reference]
        )
    ]
    if changed_footprints:
        details = [
            describe_footprint_signature_change(
                reference,
                original_footprints[reference],
                actual_footprints[reference],
            )
            for reference in changed_footprints[:12]
        ]
        raise RuntimeError(
            "SES import changed footprint identity/placement/DNP/pads: "
            + ", ".join(details)
        )
    if "AE1" not in actual_footprints:
        raise RuntimeError("AE1 NFC loop footprint was lost")
    if len(actual_footprints["AE1"][-1]) < 2:
        raise RuntimeError("AE1 no longer has its two antenna net pads")

    actual_nets = board_net_names(board)
    if actual_nets != original_nets:
        raise RuntimeError(
            "Net round-trip mismatch; missing="
            + repr(sorted(original_nets - actual_nets))
            + ", extra="
            + repr(sorted(actual_nets - original_nets))
        )
    if zone_snapshot(board) != original_zones:
        raise RuntimeError("SES import changed plane-zone net/layer assignments")

    actual_tracks = track_snapshot(board)
    lost_tracks = original_tracks - actual_tracks
    if lost_tracks:
        raise RuntimeError(
            f"SES import replaced or changed {sum(lost_tracks.values())} existing tracks"
        )
    added_tracks = actual_tracks - original_tracks
    forbidden_added_tracks = Counter(
        {
            signature: count
            for signature, count in added_tracks.items()
            if str(signature[1]).lstrip("/") in MANUAL_LOGICAL_NETS
        }
    )
    if forbidden_added_tracks:
        examples = ", ".join(
            f"{signature[0]}:{signature[1]} x{count}"
            for signature, count in list(forbidden_added_tracks.items())[:12]
        )
        raise RuntimeError(
            "SES import added tracks/vias on nets reserved for manual routing: "
            + examples
        )
    validate_no_new_inner_tracks(original_tracks, actual_tracks)
    if not allow_existing_critical_vias:
        validate_no_critical_vias(board)
    validate_four_layer_plane_stack(board)
    validate_required_planes(board)
    validate_netclasses(board, assignments)


def save_and_reload_validated(
    board,
    candidate_path: Path,
    original_footprints: dict[str, tuple[object, ...]],
    original_nets: frozenset[str],
    original_tracks: Counter[tuple[object, ...]],
    original_zones: Counter[tuple[object, ...]],
    assignments: dict[str, NetClassSpec],
    allow_existing_critical_vias: bool,
) -> tuple[int, int, int]:
    pcbnew.SaveBoard(str(candidate_path), board)
    if not candidate_path.is_file() or candidate_path.stat().st_size < 100:
        raise RuntimeError("KiCad did not save a usable routed board candidate")
    reloaded = pcbnew.LoadBoard(str(candidate_path))
    validate_round_trip(
        reloaded,
        original_footprints,
        original_nets,
        original_tracks,
        original_zones,
        assignments,
        allow_existing_critical_vias,
    )
    return (
        len(list(reloaded.GetFootprints())),
        len(list(reloaded.GetTracks())),
        unconnected_edge_count(reloaded),
    )


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    load_pcbnew()
    hardware_dir = Path(__file__).resolve().parent.parent
    validate_paths(args, hardware_dir)

    input_path = args.input.expanduser().resolve()
    design_path = args.design.expanduser().resolve()
    output_path = args.output.expanduser().resolve(strict=False)
    dsn_path = args.dsn.expanduser().resolve(strict=False)
    autorouter_dsn_path = args.autorouter_dsn.expanduser().resolve(strict=False)
    ses_path = args.ses.expanduser().resolve(strict=False)
    export_only = args.export_only or (args.jar is None and not args.import_existing_ses)
    companion_pairs = project_companions(hardware_dir, output_path)

    if export_only:
        require_destination_available(dsn_path, args.force)
    elif args.import_existing_ses:
        if not dsn_path.is_file():
            raise RuntimeError(f"The DSN used for external routing is missing: {dsn_path}")
        if not ses_path.is_file():
            raise RuntimeError(f"External SES does not exist: {ses_path}")
        if ses_path.stat().st_mtime_ns < dsn_path.stat().st_mtime_ns:
            raise RuntimeError(
                "External SES is older than the DSN; refusing a potentially stale import"
            )
        for destination in (output_path,) + tuple(
            destination for _source, destination in companion_pairs
        ):
            require_destination_available(destination, args.force)
    else:
        require_destination_available(dsn_path, args.force)
        require_destination_available(autorouter_dsn_path, args.force)
        require_destination_available(ses_path, args.force)
        for destination in (output_path,) + tuple(
            destination for _source, destination in companion_pairs
        ):
            require_destination_available(destination, args.force)

    board = pcbnew.LoadBoard(str(input_path))
    layer_names = validate_four_layer_plane_stack(board)
    validate_required_planes(board)
    if not args.allow_existing_critical_vias:
        validate_no_critical_vias(board)
    if not args.allow_stale_design_parity:
        validate_design_parity(board, design_path)
    original_footprints = footprint_snapshot(board)
    original_nets = board_net_names(board)
    original_tracks = track_snapshot(board)
    original_zones = zone_snapshot(board)
    if "AE1" not in original_footprints:
        raise RuntimeError("Input board must contain the AE1 PCB NFC-loop footprint")
    if original_tracks and not args.allow_existing_tracks:
        raise RuntimeError(
            f"Input contains {sum(original_tracks.values())} existing tracks. Refusing to "
            "let an autorouter touch them without --allow-existing-tracks."
        )

    assignments = configure_netclasses(board)
    print(
        f"Input validated: 4 layers ({', '.join(layer_names)}), "
        f"{len(original_footprints)} footprints, {len(original_nets)} nets, "
        f"{sum(original_tracks.values())} existing tracks"
    )

    with tempfile.TemporaryDirectory(prefix="PocketLabCard-routing-") as temporary_dir:
        scratch = Path(temporary_dir)

        if args.import_existing_ses:
            validate_exported_dsn(dsn_path, layer_names)
            current_dsn = scratch / "PocketLab-Card-current.dsn"
            if not pcbnew.ExportSpecctraDSN(board, str(current_dsn)):
                raise RuntimeError("KiCad Specctra DSN comparison export failed")
            normalize_dsn_identity(current_dsn, dsn_path.name)
            validate_exported_dsn(current_dsn, layer_names)
            if current_dsn.read_bytes() != dsn_path.read_bytes():
                raise RuntimeError(
                    "Published DSN does not exactly match the current placement board/netclasses; "
                    "export it again before importing this SES"
                )
            import_ses = ses_path
        else:
            scratch_dsn = scratch / "PocketLab-Card-routing.dsn"
            if not pcbnew.ExportSpecctraDSN(board, str(scratch_dsn)):
                raise RuntimeError("KiCad Specctra DSN export failed")
            normalize_dsn_identity(scratch_dsn, dsn_path.name)
            validate_exported_dsn(scratch_dsn, layer_names)

            if export_only:
                publish_file(scratch_dsn, dsn_path, args.force)
                print(f"DSN exported: {dsn_path}")
                print(
                    "No SES was imported. Route this DSN in FreeRouting and then run "
                    "with --import-existing-ses."
                )
                print(
                    "L2/L3 are exported as type 'power'; no explicit cross-router "
                    "routable=false field exists, so the later import is checked again."
                )
                return 0

            java = resolve_java(args.java)
            jar = args.jar.expanduser().resolve()
            validate_freerouting_jar(java, jar)
            scratch_autorouter_dsn = scratch / "PocketLab-Card-routing-digital.dsn"
            removed_count, retained_count = filter_autorouter_network(
                scratch_dsn, scratch_autorouter_dsn
            )
            print(
                f"Guarded autorouter DSN: removed {removed_count} protected nets; "
                f"retained {retained_count} routable nets"
            )
            scratch_ses = scratch / "PocketLab-Card-routing.ses"
            run_freerouting(
                java,
                jar,
                scratch_autorouter_dsn,
                scratch_ses,
                args.passes,
                args.threads,
            )
            promoted_paths = normalize_freerouting_session(scratch_ses)
            print(
                f"SES width audit: promoted {promoted_paths} autorouter neckdowns "
                "to the 0.20-mm project minimum"
            )
            import_ses = scratch_ses

        if not pcbnew.ImportSpecctraSES(board, str(import_ses)):
            raise RuntimeError(f"KiCad failed to import Specctra SES: {import_ses}")
        # The imported tracks and vias invalidate the serialized plane fill.
        # Refill before validation/publication so signal vias receive their
        # real antipads instead of producing zero-clearance stale-zone errors.
        if not pcbnew.ZONE_FILLER(board).Fill(board.Zones()):
            raise RuntimeError("KiCad failed to refill plane zones after SES import")
        validate_round_trip(
            board,
            original_footprints,
            original_nets,
            original_tracks,
            original_zones,
            assignments,
            args.allow_existing_critical_vias,
        )

        candidate_board = scratch / "PocketLab-Card-routed.kicad_pcb"
        footprint_count, track_count, unconnected_count = save_and_reload_validated(
            board,
            candidate_board,
            original_footprints,
            original_nets,
            original_tracks,
            original_zones,
            assignments,
            args.allow_existing_critical_vias,
        )

        # Publish only after the imported board has also survived a KiCad
        # save/reload round trip.  The main project PCB is protected above.
        if not args.import_existing_ses:
            publish_file(scratch_dsn, dsn_path, args.force)
            publish_file(scratch_autorouter_dsn, autorouter_dsn_path, args.force)
            publish_file(import_ses, ses_path, args.force)
        publish_board_bundle(
            candidate_board,
            output_path,
            companion_pairs,
            args.force,
        )

    print(f"Routed staging board: {output_path}")
    print(
        "Matching KiCad companions: "
        + ", ".join(str(destination) for _source, destination in companion_pairs)
    )
    print(
        f"Round trip retained {footprint_count} footprints (including AE1 and DNP state), "
        f"{len(original_nets)} nets and produced {track_count} tracks/vias."
    )
    print(f"Remaining KiCad ratsnest edges (unconnected): {unconnected_count}")
    print(
        "Validated: no new planar tracks on L2/L3 and no pre-existing track was lost."
    )
    print(
        "REVIEW REQUIRED: manually inspect USB pair geometry, RF impedance/antenna, NFC "
        "matching, power/switch-node routing, plane fills and return paths; then run KiCad DRC."
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RuntimeError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        raise SystemExit(2)
