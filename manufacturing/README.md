# BomberCard manufacturing handoff

This directory contains the reproducible KiCad 10 export pipeline. Generated
files are intentionally ignored by Git; reviewed release archives are created
as `manufacturing/BomberCard-<release>.zip`.

> **Current status:** `hardware/PocketLab-Card.kicad_pcb` is still marked as a
> placement/routing draft. It is not order-ready. The exporter runs ERC and DRC
> and will currently stop before creating Gerbers. Do not upload the old board
> merely because KiCad can display it.

## Run the checks and export

Run from the repository root with KiCad 10 installed:

```powershell
# Reports and release gates only
powershell -ExecutionPolicy Bypass -File .\manufacturing\scripts\export_release.ps1 `
  -ReleaseName v1-proto -ChecksOnly -Force

# Complete fabrication handoff after every release gate passes
powershell -ExecutionPolicy Bypass -File .\manufacturing\scripts\export_release.ps1 `
  -ReleaseName v1-proto -Force

# PCBA handoff: additionally require Manufacturer, MPN and LCSC for every CPL row
powershell -ExecutionPolicy Bypass -File .\manufacturing\scripts\export_release.ps1 `
  -ReleaseName v1-proto -RequireCompleteProcurement -Force
```

If `kicad-cli` is not on `PATH`, the script finds the normal KiCad 10 Windows
installation. A nonstandard installation can be supplied with
`-KiCadCli 'C:\path\to\kicad-cli.exe'`. Releases must be made from a clean Git
working tree. `-AllowDirty` exists only for local draft checks and must not be
used for an ordered revision.

The pipeline performs these steps in order:

1. KiCad ERC, including all severities in `reports/erc.json`.
2. KiCad DRC with zone refill in memory and schematic/PCB parity checking.
3. A release gate that rejects ERC/DRC errors, every schematic-parity mismatch,
   a dirty source tree, and the explicit placement-draft marker.
4. Master and JLCPCB BOMs, JLCPCB-format CPL, manual-assembly list and a separate
   procurement-gap report.
5. Four copper Gerbers, masks, paste, silkscreen and `Edge.Cuts`; separate PTH/
   NPTH Excellon drills and a drill map.
6. IPC-D-356 netlist, board statistics, source snapshots, SHA-256 manifest and a
   release ZIP plus its checksum.

Warnings remain visible in the JSON reports but do not automatically disappear
or become approvals. Review every warning before ordering. PCB/schematic parity
is deliberately stricter: any mismatch blocks the archive even if KiCad labels
it a warning.

## DNP and assembly data

The exporter builds one effective do-not-populate list from every truthy `DNP`
field in the raw KiCad schematic BOM plus a safety-default list. Truthy values
include `true`, `yes`, `1`, `dnp` and `x`. It enforces the resulting list
independently of PCB-footprint DNP flags, so a missing board flag cannot put an
optional part back into either the JLC BOM or CPL.

The safety defaults are:

- `C109`: optional VSYS bulk capacitor; populate only after a stability and
  transient test shows it is required and safe.
- `C310`, `C311`, `C312`: NFC matching options; leave open until the real PCB
  antenna has been measured and matching values have been selected.
- `C403`, `C404`: Sub-GHz pi-match shunt options; leave open until the assembled
  RF path and spring antenna have been measured.
- `C507`, `C508`, `C509`: LF resonance/RX trim options; leave open until the
  actual external coil has been measured and the receive path checked.

They are marked in `bom-master.csv` and removed from both `bom-jlcpcb.csv` and
`cpl-jlcpcb.csv`. Any additional schematic part carrying a truthy `DNP` field is
handled the same way. The effective list is recorded in `release-check.txt` and
`release-info.json`. The master BOM always contains the columns `Manufacturer`,
`MPN` and `LCSC`; empty fields are not silently invented. For an assembled order,
use `-RequireCompleteProcurement`; the command blocks while any CPL part lacks
one of those three identifiers.

`bom-jlcpcb.csv` contains only designators also present in the generated SMT
position file. Through-hole, mixed-pad and explicitly manual parts are listed in
`bom-manual-or-review.csv`. The file `hardware/assembly-variants.csv` is a design
planning aid, not yet a complete variant definition for every dependent passive.
Do not create a cost-down variant simply by deleting its main IC from the BOM.

JLCPCB rotations are not universal library data. Review **every** pin-1,
polarized, connector, LED, diode, QFN/LGA and module orientation in JLCPCB's 2D
assembly viewer against KiCad before accepting the order. The generated CPL is a
coordinate export, not that visual sign-off.

## Suggested JLCPCB prototype settings

These are ordering targets, not a substitute for matching the final KiCad board
stack-up and design rules:

| Setting | Prototype target |
| --- | --- |
| Base material | FR-4 |
| Layer count | 4 |
| Board thickness | 1.2 mm |
| Finished outline | 85.60 x 53.98 mm, including the specified corner radii |
| Copper weight | JLC04121H-7628 target: 1 oz outer, 0.5 oz inner |
| Surface finish | ENIG recommended for the fine-pitch/QFN/LGA prototype |
| Solder mask | Both sides; color is optional |
| Silkscreen | Both sides if present in the reviewed Gerbers |
| Via treatment | Tented by default; explicitly review RF, thermal and test vias |
| Impedance | Do not select a generic value blindly; use the chosen JLC 4-layer stack-up and recalculate RF traces |
| Assembly side | Two-sided PCBA required by the dense card layout; review both CPL sides and the added setup cost |
| Quantity | Five boards is a sensible first characterization batch |

Select the exact `JLC04121H-7628` structure in the quote, then compare
JLCPCB's live stack-up table with the board file before the
final RF routing. Enter its dielectric heights and copper thicknesses in KiCad,
then recalculate the 50-ohm Sub-GHz feed geometry. The
E07-900MM10S pin-6 antenna path uses a tuneable pi network and the inboard
T3-868M spring; both require measurement with the assembled mechanical stack.
If the final stack-up is
not represented in the board file, the fabrication ZIP must not be released.

Keep the credit-card outline free of factory tooling holes. If panel rails,
fiducials or tooling holes are needed, request them on removable rails and check
USB, optical and antenna edge clearances in the DFM viewer. Do not allow an
automatic outline change without comparing the returned production file.

## Mandatory prototype validation

Passing ERC/DRC only proves consistency against the encoded rules. It does not
prove electrical, RF, thermal or battery safety. Before a larger build:

- Power the first board from a current-limited supply without a LiPo. Validate
  shorts, USB inrush, charger/power-path behavior, 3.3 V stability, 5 V enable/
  disable and load transients. Fit `C109` only from measured evidence.
- Verify LiPo polarity, protection cutoffs, charge current, temperature behavior
  and no back-powering through USB, headers or peripherals before unattended
  charging.
- Characterize the NFC loop on the finished board with suitable RF/VNA or NFC
  measurement equipment. Select `C310`-`C312` and the main matching network from
  measurements, then re-run ERC/DRC and issue a new revision.
- Validate the Sub-GHz antenna/feed/connector with the enclosure and intended
  antenna. Regulatory limits, permitted bands and transmit power depend on the
  country and use case.
- Validate the external LF coil only after measuring its inductance and fitting
  reviewed values for `C506`-`C509`. Check HTRC phase, current, resonant voltage,
  ANTFAIL behavior and read range before extended operation.
- Inspect all fine-pitch and exposed-pad devices, then exercise USB, microSD,
  LF RFID, IR, NFC, sensors and every expansion voltage under load.

Only after those results are recorded should the project lose its
`prototype_only` status. The exporter intentionally writes
`production_approved: false` and `rf_and_nfc_tuning_required: true` into every
release metadata file; a ZIP by itself is never approval to mass-produce.
