"""Build the netlisted, placement-only PocketLab Card PCB staging file.

The schematic generator is the single source of truth for references,
footprints and pin-to-net assignments.  This script deliberately stops before
routing: it loads the exact mechanical card outline, places every populated
footprint, assigns every schematic net to its pads and writes a separate
``PocketLab-Card-netlisted.kicad_pcb`` file.

Run with KiCad's bundled Python, for example::

    "C:/Program Files/KiCad/10.0/bin/python.exe" hardware/scripts/build_pcb.py

The script refuses to overwrite the project's main PCB.  Its placement checks
are intentionally conservative AABB checks around F/B courtyard geometry.
They are not a replacement for KiCad DRC or a physical prototype review.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

import pcbnew


BOARD_LEFT = 20.0
BOARD_TOP = 20.0
BOARD_RIGHT = 105.6
BOARD_BOTTOM = 73.98
NORMAL_INSET = 0.8
# KiCad courtyards already include package-specific assembly clearance.  This
# is only an extra numerical/placement margin between those courtyards.
PLACEMENT_CLEARANCE = 0.02


@dataclass(frozen=True)
class Box:
    left: float
    top: float
    right: float
    bottom: float

    @property
    def width(self) -> float:
        return self.right - self.left

    @property
    def height(self) -> float:
        return self.bottom - self.top

    def expanded(self, amount: float) -> "Box":
        return Box(
            self.left - amount,
            self.top - amount,
            self.right + amount,
            self.bottom + amount,
        )

    def intersects(self, other: "Box") -> bool:
        return not (
            self.right <= other.left
            or self.left >= other.right
            or self.bottom <= other.top
            or self.top >= other.bottom
        )

    def inside(self, other: "Box") -> bool:
        return (
            self.left >= other.left
            and self.top >= other.top
            and self.right <= other.right
            and self.bottom <= other.bottom
        )


@dataclass(frozen=True)
class Region:
    side: str
    box: Box


@dataclass(frozen=True)
class FixedPlacement:
    reference: str
    side: str
    x_mm: float
    y_mm: float
    rotation_deg: float = 0.0
    use_origin: bool = False
    allow_edge: bool = False
    blocks_both_sides: bool = False


@dataclass(frozen=True)
class Occupied:
    reference: str
    side: str
    box: Box


# AE1 contains the complete four-turn winding.  Its 36.1 x 29.1 mm physical
# envelope is a component keepout on *both* sides, including the inner opening;
# this is stricter than merely reserving the copper annulus.
NFC_OUTER = Box(22.85, 23.45, 58.95, 52.55)

# The project-local U1 footprint has a physical assembly courtyard, while its
# original all-layer antenna rule area remains unchanged.  Keep this explicit
# body model as an independent placement assertion and reserve the antenna end
# on both PCB sides.
ESP_BODY = Box(83.5, 20.0, 101.7, 45.6)
# Stock ESP32-S3-WROOM-1 embeds an all-layer footprint keepout extending 24 mm
# left/right around its antenna end.  At the fixed module position only the
# board-interior portion below is relevant.
ESP_ANTENNA = Box(68.5, 20.0, 105.6, 26.2)

# The complete 17-mm coil plus its approximately 2-mm lead lies inside an
# open-bottom pocket.  The original 85.60 x 53.98-mm outer envelope remains
# unchanged and the solder land sits directly at the pocket's right end.
SUBGHZ_ANTENNA_RESERVE = Box(66.0, 67.2, 88.25, BOARD_BOTTOM)
SUBGHZ_NOTCH_LEFT = 66.2
SUBGHZ_NOTCH_RIGHT = 85.0
SUBGHZ_NOTCH_TOP = 67.4

# U17 and its hand-solderable support parts share this front-side island with
# the 0.50-mm LF_5V distribution escape.  Keep unrelated auto-placed GPIO and
# header resistors out so a regenerated placement cannot silently block the
# reviewed power corridor.
LF_SUPPORT_ROUTING_RESERVE = Box(34.0, 53.0, 47.4, 61.1)
LF_SUPPORT_REFERENCES = frozenset({"U17", "R501", "C501", "C502", "R506", "U24"})


FIXED_PLACEMENTS: tuple[FixedPlacement, ...] = (
    FixedPlacement("AE1", "F", 40.9, 38.0, 0.0, False, False, True),
    # RF modules and their external antenna connectors.
    # Shift U1 0.40 mm left to create a manufacturable front-side microSD
    # protection strip.  R203 follows by the same amount.
    FixedPlacement("U1", "F", 92.6, 32.75, 0.0, True, True, False),
    # Keep the complete HTRC110 resonant/RX island on the back.  The custom
    # rules deliberately forbid vias on these sensitive nets, so U4 must share
    # a side with the removable 400-uH coil connector and every analogue part.
    # Vertical SO14 orientation puts TX1/TX2/CLK on the coil side and the
    # three analogue conditioning pins on the open right edge.  Raising the
    # package to the NFC-reserve boundary leaves a straight tuning bank below.
    FixedPlacement("U4", "B", 43.0, 57.22, 180.0),
    # The LF 5-V switch is not part of the resonant loop and uses the front-side
    # pocket vacated by U4; its power/control nets may use ordinary vias.
    FixedPlacement("U17", "F", 40.5, 55.5),
    FixedPlacement("J3", "B", 36.2, 63.5, 0.0, False, False, False),
    # Front-side LF 5-V load-switch support, arranged for short hand-solderable
    # power and control escapes inside the reserved corridor.
    FixedPlacement("R501", "F", 43.75, 55.5),
    FixedPlacement("C501", "F", 39.0, 59.5, 180.0),
    FixedPlacement("C502", "F", 44.50, 58.8, 180.0),
    FixedPlacement("R506", "F", 36.5, 55.5, 180.0),
    # Fitted resonance parts form the shortest possible path from J3 to U4.
    # CEXT/QGND/RX conditioning sits immediately above the SO14, while the two
    # optional tuning capacitors remain accessible along its right edge.
    FixedPlacement("R502", "B", 40.37, 64.2, 180.0),
    FixedPlacement("C505", "B", 35.7, 58.0, 90.0),
    # Three parallel tuning capacitors form two straight buses below U4:
    # TX2 on their upper pads, TAP on their lower pads.  C507/C508 are DNP.
    FixedPlacement("C506", "B", 43.74, 64.2, 90.0),
    FixedPlacement("R503", "B", 35.8, 53.7),
    FixedPlacement("R504", "B", 52.70, 57.50),
    FixedPlacement("C503", "B", 48.55, 56.10, 180.0),
    FixedPlacement("C504", "B", 52.30, 54.30, 180.0),
    FixedPlacement("C507", "B", 45.60, 64.2, 90.0),
    FixedPlacement("C508", "B", 47.20, 64.2, 90.0),
    FixedPlacement("C509", "B", 49.50, 58.78, 90.0),
    # High-voltage TAP probing stays beside the tuning bank, clear of J5/U12.
    FixedPlacement("TP502", "B", 47.20, 67.00),
    # Preserve accessible power probing without occupying the back-side LF
    # island.  These are low-speed service pads and therefore live on F.Cu.
    FixedPlacement("TP104", "F", 36.0, 72.0),
    FixedPlacement("TP107", "F", 41.0, 62.2),
    # Keep the displaced microSD pull-up in the back-side row between the LF
    # trim bank and clock source; the other two pull-ups retain their sites.
    FixedPlacement("R514", "B", 62.0, 57.8),
    FixedPlacement("R515", "B", 65.0, 57.8, 90.0),
    # Re-home the displaced low-speed pull-ups, GPIO protection parts and
    # service test points in their former LF auto-placement sites.
    FixedPlacement("R517", "F", 33.74, 21.82),
    FixedPlacement("R518", "F", 43.24, 21.82),
    FixedPlacement("R519", "F", 50.74, 21.82),
    FixedPlacement("R713", "F", 25.75, 21.82),
    FixedPlacement("R714", "F", 29.75, 21.82),
    FixedPlacement("R720", "F", 54.74, 21.82),
    FixedPlacement("R721", "F", 65.42, 30.89),
    FixedPlacement("TP201", "F", 61.42, 33.49),
    FixedPlacement("TP202", "F", 68.25, 30.95),
    FixedPlacement("TP203", "F", 61.45, 29.43),
    # Keep the 4-MHz HTRC clock local while using the open strip above the
    # Dupont matrix. The logic translators fit in the remaining front-side
    # support area; the secure element mirrors the RTC on the opposite
    # side of the board without violating either component courtyard.
    FixedPlacement("Y501", "B", 60.3, 54.7),
    FixedPlacement("C513", "B", 64.95, 54.7),
    FixedPlacement("U21", "F", 73.9, 34.0),
    FixedPlacement("C515", "F", 73.7, 28.5),
    FixedPlacement("U22", "F", 75.0, 43.0),
    FixedPlacement("C516", "F", 80.5, 43.0),
    FixedPlacement("U23", "F", 42.0, 69.0),
    FixedPlacement("C517", "F", 47.1, 69.0, 90.0),
    FixedPlacement("R509", "F", 101.0, 64.5),
    # The smaller module's ANT castellated pad faces its hand-tuneable pi
    # network and the lower-edge spring antenna.
    FixedPlacement("U3", "B", 73.0, 58.5, 0.0, False, False, False),
    FixedPlacement("C403", "B", 75.4, 66.0),
    # Put pad 1 toward U3/C403 and pad 2 toward C404/antenna so the 868-MHz
    # series element does not force the RF feed to cross back over itself.
    FixedPlacement("R405", "B", 79.1, 66.0, 180.0),
    FixedPlacement("C404", "B", 82.8, 66.0),
    FixedPlacement("A1", "F", 86.8, 70.45, 0.0, True, True, True),
    # Edge connectors and through-hole user interfaces.
    FixedPlacement("J1", "F", 102.6, 51.7, 90.0, True, True, True),
    # Rotate the USB protector so its N/P pins face J1 in the same physical
    # order as the MCU pair.  The two vertical 22-ohm resistors convert U1's
    # stacked N/P pin order into a left/right pair without a crossing or via;
    # pad 2 faces the MCU and pad 1 faces the protection device.
    FixedPlacement("U16", "F", 93.45, 50.0, 270.0),
    # Keep the B5 sink pull-down inside the narrow protector/connector channel.
    # J8 occupies the full front-side space above U16, so the separately packed
    # A5 pull-down remains on the back beside the right-edge controls.  Both
    # parts remain hand-solderable 0805s.
    FixedPlacement("R102", "F", 96.24, 47.70, 270.0),
    FixedPlacement("R201", "F", 84.3, 48.15, 90.0),
    FixedPlacement("R202", "F", 86.56, 48.15, 90.0),
    FixedPlacement("C314", "F", 89.845, 47.025),
    # Keep the ESP32 enable network and supply decoupling beside the module
    # instead of allowing them to occupy the USB breakout channel.
    FixedPlacement("C202", "F", 81.2, 28.0, 180.0),
    FixedPlacement("C203", "F", 77.5, 28.0, 180.0),
    FixedPlacement("R203", "F", 81.4, 31.4, 180.0),
    FixedPlacement("C201", "F", 79.8, 33.7, 180.0),
    # Mount the side-entry LiPo connector on the back. Relative to the former
    # front-side orientation it is turned 90 degrees counter-clockwise so the
    # cable no longer occupies the visible front face.
    FixedPlacement("J4", "B", 102.1, 63.0, 180.0, True, True, False),
    # The freed buzzer GPIO becomes a single 2.54-mm Dupont point at the
    # lower-right edge.  J5.28 now carries the optional charger-NTC input.
    FixedPlacement("J5", "F", 56.0, 67.0, 0.0, True, True, True),
    FixedPlacement("SW6", "F", 102.5, 69.5, 0.0, False, True, False),
    # Factory-mounted side-view emitters face the left card edge.  Their
    # 1.2-mm bodies stay inboard while their narrow optical axes remain clear.
    FixedPlacement("D1", "F", 21.65, 55.78, 90.0, True, True, True),
    FixedPlacement("D2", "F", 21.65, 62.20, 90.0, True, True, True),
    FixedPlacement("D3", "F", 21.65, 68.62, 90.0, True, True, True),
    # The side-view receiver uses the free top-edge window.  Its optical face
    # is 0.325 mm inside the original card outline and its complete physical
    # body/pad row remains inboard; only the normal edge courtyard is relaxed.
    FixedPlacement("U13", "F", 63.0, 21.5, 0.0, True, True, False),
    FixedPlacement("R604", "F", 60.8, 26.5, 90.0),
    FixedPlacement("C601", "F", 63.2, 26.5, 90.0),
    FixedPlacement("C602", "F", 65.6, 26.5, 90.0),
    # Edge-access microSD under the ESP body.  The reviewed 0.175-mm inward
    # adjustment keeps its electrical and shell pads at the configured
    # 0.50-mm copper-to-edge clearance; the card intentionally projects out.
    FixedPlacement("J2", "B", 99.10, 39.0, 90.0, False, True, False),
    # NFC controller just outside the loop annulus.
    FixedPlacement("U2", "F", 65.0, 42.6),
    # Keep the 27.12 MHz Pierce oscillator directly below U2.  The two load
    # capacitors face the crystal signal pads while their ground pads remain
    # exposed for very short stitching vias into the L2 ground plane.
    FixedPlacement("Y301", "F", 65.0, 48.4),
    FixedPlacement("C306", "F", 61.8, 48.1, 90.0),
    FixedPlacement("C307", "F", 68.2, 48.1, 270.0),
    FixedPlacement("L301", "F", 61.0, 37.0, 90.0),
    FixedPlacement("L302", "F", 65.0, 37.0, 90.0),
    FixedPlacement("C308", "F", 68.0, 36.0),
    FixedPlacement("C309", "F", 68.0, 33.0),
    # C311 is a DNP NFC tuning option.  Fix it in the NFC bank so the generic
    # placer cannot put it directly in front of U1's USB pins.
    FixedPlacement("C311", "F", 78.0, 38.5),
    FixedPlacement("R303", "F", 47.0, 22.2),
    FixedPlacement("R304", "F", 39.5, 22.2),
    # Visible controls occupy the strip below the complete NFC keepout.
    FixedPlacement("LED1", "F", 49.0, 59.2, 180.0),
    FixedPlacement("LED2", "F", 53.0, 59.2, 180.0),
    FixedPlacement("LED3", "F", 57.0, 59.2, 180.0),
    FixedPlacement("LED4", "F", 61.0, 59.2, 180.0),
    # Source termination belongs at U20.Y, not at the first LED after the
    # roughly 25-mm trace. Pin 1 sits about 2.0 mm from buffer output pin 4.
    FixedPlacement("U20", "F", 65.8, 60.75, 180.0),
    FixedPlacement("C609", "B", 65.5, 65.9),
    FixedPlacement("SW3", "F", 51.0, 55.15),
    FixedPlacement("SW7", "F", 57.0, 55.15),
    FixedPlacement("SW4", "F", 63.0, 55.15),
    FixedPlacement("R607", "F", 68.70, 50.95),
    FixedPlacement("R608", "B", 55.25, 59.0, 90.0),
    FixedPlacement("R609", "B", 55.7, 55.5),
    # Strap/NFC test pads remain accessible on the front, but outside the
    # coupled USB escape corridor along U1's lower-left corner.
    # ESP service buttons, bare OLED and high-current IR hardware.  J8's FPC
    # is soldered first and folded under the 12 x 11 mm glass.  The charge-pump
    # capacitors form an L around the panel and remain accessible for rework.
    FixedPlacement("SW1", "F", 72.0, 48.0),
    FixedPlacement("SW2", "F", 78.0, 48.0),
    # Rotating the panel puts its folded FPC lands beside this two-column
    # capacitor bank instead of across the USB breakout.
    FixedPlacement("J8", "F", 90.55, 58.5, 180.0),
    FixedPlacement("C610", "F", 83.15, 54.55, 90.0),
    FixedPlacement("C611", "F", 83.15, 58.30, 90.0),
    FixedPlacement("C612", "F", 80.85, 54.55, 90.0),
    FixedPlacement("C613", "F", 80.85, 58.30, 90.0),
    FixedPlacement("C614", "F", 83.15, 62.05, 90.0),
    FixedPlacement("C615", "F", 80.85, 62.05, 90.0),
    FixedPlacement("C616", "B", 81.0, 62.2),
    # The slide actuator faces the lower card edge.  Switching U6 EN rather
    # than the battery current path keeps the small switch within its rating.
    FixedPlacement("SW5", "F", 92.9, 69.45),
    # Keep each compact ballast resistor directly behind its edge emitter.
    # The pulse reservoir and MOSFET remain in the shared high-current island.
    FixedPlacement("R601", "F", 25.0, 55.78, 0.0),
    FixedPlacement("C607", "F", 70.65, 61.50),
    FixedPlacement("C617", "B", 65.0, 62.15, 90.0),
    FixedPlacement("C608", "F", 64.825, 68.55, 90.0),
    FixedPlacement("R610", "F", 25.0, 62.20, 0.0),
    FixedPlacement("R611", "F", 25.0, 68.62, 0.0),
    FixedPlacement("Q1", "F", 69.0, 64.8, 270.0),
    FixedPlacement("R602", "F", 66.2, 64.8, 90.0),
    FixedPlacement("R603", "F", 71.8, 64.8, 90.0),
    # U3 local SPI and supply parts sit on F.Cu opposite its rotated digital
    # row.  Short vias land directly beside the module pads on B.Cu.
    FixedPlacement("R403", "F", 69.0, 53.0),
    FixedPlacement("R406", "F", 73.0, 53.0, 180.0),
    FixedPlacement("R402", "F", 69.8, 56.0, 90.0),
    FixedPlacement("R401", "F", 72.2, 56.0, 90.0),
    FixedPlacement("R404", "F", 74.6, 56.0, 90.0),
    # USB input protection and charger.  The high-frequency IN/BAT/OUT
    # capacitors face their U5 pins; programming and default resistors remain
    # in the same right-side power island rather than at the far board edge.
    FixedPlacement("F1", "B", 94.9, 47.1),
    FixedPlacement("D101", "B", 90.2, 47.1),
    # Pin 13 (VBUS) faces the connector-side capacitors, CELL_POS faces the
    # two-capacitor row below, and VSYS faces the converter-side capacitors.
    FixedPlacement("U5", "B", 90.2, 50.5, 90.0),
    FixedPlacement("C103", "B", 94.15, 52.0, 180.0),
    FixedPlacement("C106", "B", 94.15, 49.75, 180.0),
    FixedPlacement("C104", "B", 92.4, 54.1),
    FixedPlacement("C121", "B", 88.8, 54.1),
    FixedPlacement("C105", "B", 86.9, 50.5, 90.0),
    FixedPlacement("C122", "B", 86.8, 46.8, 90.0),
    # Keep the four analogue charger-programming resistors immediately below
    # U5.  Quiet status/default pulls remain with the region placer so the
    # reserved microSD bulk-capacitor slot is not consumed by fixed power parts.
    FixedPlacement("R111", "B", 85.2, 54.0),
    FixedPlacement("R112", "B", 85.2, 56.2),
    FixedPlacement("R113", "B", 88.9, 57.2),
    FixedPlacement("R114", "B", 92.6, 57.2),
    FixedPlacement("R128", "B", 90.6, 32.0),
    # TPS63070: retain the compact dual switch-node geometry and move the HF
    # bypasses plus control/feedback components into the newly opened gap.
    FixedPlacement("U6", "B", 67.0, 44.0),
    FixedPlacement("L6", "B", 62.5, 44.0, 90.0),
    FixedPlacement("C108", "B", 62.0, 39.8),
    FixedPlacement("C123", "B", 66.8, 40.8, 180.0),
    FixedPlacement("C124", "B", 67.95, 47.85, 270.0),
    FixedPlacement("C107", "B", 70.55, 44.6, 180.0),
    FixedPlacement("R116", "B", 70.3, 41.2, 90.0),
    FixedPlacement("R117", "B", 70.4, 47.4, 90.0),
    FixedPlacement("R118", "B", 73.15, 47.7, 180.0),
    FixedPlacement("R119", "B", 72.8, 41.6, 90.0),
    FixedPlacement("C110", "B", 60.8, 49.8),
    FixedPlacement("C112", "B", 68.5, 51.1),
    # TPS61023: move the complete island right, freeing U6's quiet feedback
    # side.  C113 is physically parallel to R120 and both divider parts face
    # U7.1; R403/R404 are moved below the island.
    FixedPlacement("U7", "B", 81.5, 44.0),
    FixedPlacement("L7", "B", 77.0, 44.0),
    FixedPlacement("C113", "B", 86.4, 41.8, 270.0),
    FixedPlacement("R120", "B", 84.5, 41.8, 270.0),
    FixedPlacement("R121", "B", 84.5, 45.5, 270.0),
    FixedPlacement("R122", "B", 89.75, 44.5, 180.0),
    FixedPlacement("R129", "B", 84.2, 49.1, 90.0),
    FixedPlacement("C114", "B", 81.1, 47.5),
    FixedPlacement("C115", "B", 81.3, 41.65, 180.0),
    FixedPlacement("C116", "B", 77.4, 51.1),
    # Header load switch, current-limit resistor and bypasses form their own
    # compact island.  Rotation puts IN on the left and OUT on the right.
    FixedPlacement("U15", "B", 94.5, 60.3, 180.0),
    FixedPlacement("C117", "B", 90.63, 61.27),
    FixedPlacement("C119", "B", 95.84, 63.83, 270.0),
    FixedPlacement("C118", "B", 100.0, 70.4, 270.0),
    FixedPlacement("R123", "B", 95.64, 56.79, 90.0),
    FixedPlacement("R124", "B", 88.1, 37.0, 90.0),
    FixedPlacement("R127", "B", 90.65, 59.22),
    FixedPlacement("U8", "B", 82.0, 36.5),
    FixedPlacement("C120", "B", 81.7, 39.3),
    # Cell protection follows the physical current order
    # GND -> Q3 -> common drains -> Q2 -> CELL_NEG/J4.
    FixedPlacement("U14", "B", 88.0, 65.5),
    FixedPlacement("C102", "B", 85.6, 65.4, 270.0),
    FixedPlacement("R104", "B", 91.0, 65.6),
    FixedPlacement("R105", "B", 85.2, 62.3),
    # Keep the protection MOSFET pair to the right of the completely inboard
    # spring-antenna pocket.  J4 is raised slightly so the complete protection
    # chain still remains compact and the RF keepout is not compromised.
    FixedPlacement("Q3", "B", 90.65, 71.8, 180.0, False, True),
    FixedPlacement("Q2", "B", 95.2, 71.8, 0.0, False, True),
    FixedPlacement("R107", "B", 90.4, 67.8),
    FixedPlacement("R106", "B", 94.7, 67.8),
    # Sensors and expanders use the upper-right back side; the complete NFC
    # loop opening remains empty on both sides.  Keep every local bypass part
    # beside its IC instead of leaving it to the generic block placer.
    FixedPlacement("U9", "B", 64.0, 31.0),
    FixedPlacement("C701", "B", 61.3, 24.8, 90.0),
    FixedPlacement("R703", "B", 64.8, 24.5),
    FixedPlacement("U18", "B", 75.0, 29.5),
    FixedPlacement("C707", "B", 80.95, 27.5, 180.0),
    FixedPlacement("R701", "B", 69.2, 28.0, 90.0),
    FixedPlacement("R702", "B", 69.2, 31.8, 90.0),
    FixedPlacement("U10", "B", 72.0, 36.0),
    FixedPlacement("C703", "B", 72.0, 33.317, 180.0),
    FixedPlacement("C702", "B", 70.5, 38.65),
    FixedPlacement("U11", "B", 78.0, 35.7),
    FixedPlacement("C704", "B", 78.0, 33.335, 180.0),
    FixedPlacement("C705", "B", 78.0, 38.2),
    # The compact 6x5 Dupont matrix also reserves its through-hole projection
    # on the back.  Keep the RTC as one short-trace island immediately left of
    # that matrix and to the right of the fully inboard IR emitter bodies.
    FixedPlacement("U12", "B", 42.0, 69.5, 180.0),
    FixedPlacement("Y701", "B", 35.5, 69.5, 270.0),
    # Moved clear of the LF resonance row while staying beside U12.
    FixedPlacement("C706", "B", 47.1, 71.4, 90.0),
    # The final 4 x 4 mm back-side pocket is used for a low-cost board NTC.
    # Both 0603 parts remain reachable for rework and sit beside the 5-V
    # converter area without crowding U25 below them.
    FixedPlacement("R736", "B", 81.2, 53.25),
    FixedPlacement("RT701", "B", 81.2, 55.25),
    # Direct MCU GPIO resistors use every remaining source-side pocket around
    # U1/J1.  The four right-edge parts stay above J4's through-hole volume;
    # the longer GPIO41-44 stubs are an explicit density compromise.
    FixedPlacement("R710", "B", 103.0, 27.75, 180.0),
    FixedPlacement("R711", "B", 99.5, 27.75),
    FixedPlacement("R712", "B", 88.75, 29.0, 90.0),
    # R713/R718 and R606 are deliberately left to the audited region placer;
    # their former strip now provides short, accessible OLED capacitor loops.
    # R714 is also left to the region placer because the OLED bank now uses
    # its former service location.
    FixedPlacement("R715", "B", 96.0, 27.75),
    FixedPlacement("R716", "F", 100.25, 59.0, 270.0),
    FixedPlacement("R717", "F", 102.5, 59.0, 270.0),
    FixedPlacement("R719", "F", 98.0, 59.0, 270.0),
    # GPIO38 now serves the dedicated PAIR button at the lower-right edge.
    # R720/R721 are left to the audited region placer because their former
    # positions are inside J8's courtyard.
    # The TCA9535 protection bank fits in the 2.82-mm top strip, outside both
    # the NFC component reservation and ESP antenna keepout.
    FixedPlacement("R722", "B", 66.6, 21.82, 180.0),
    FixedPlacement("R723", "B", 62.9, 21.82, 180.0),
    FixedPlacement("R724", "B", 59.2, 21.82),
    FixedPlacement("R725", "B", 55.5, 21.82),
    FixedPlacement("R726", "B", 51.8, 21.82),
    FixedPlacement("R727", "B", 48.1, 21.82),
    FixedPlacement("R728", "B", 44.4, 21.82),
    FixedPlacement("R729", "B", 40.7, 21.82),
    # Header-side I2C/SPI damping remains immediately above J5 while leaving
    # the LF reader and four-LED/capacitor row clear.
    # The two shunt ESD arrays sit beside their respective header-side series
    # banks. U24 uses the pocket immediately left of J5 for I2C; U25 uses the
    # back-side pocket opposite the SPI bank. No general-purpose GPIO is
    # consumed merely to fill the remaining clamp channels.
    FixedPlacement("U24", "F", 46.3, 63.0, 90.0),
    FixedPlacement("U25", "B", 81.2, 58.59, 90.0),
    # J2 crosses immediately to the front-side ESD bank. R511/R512 flank the
    # opposite-side U5 thermal vias with 0.275 mm copper clearance, keeping
    # the y=47..48 mm USB differential escape corridor unobstructed.
    FixedPlacement("U19", "F", 103.85, 38.5, 90.0, False, True, False),
    FixedPlacement("R510", "F", 81.45, 38.92, 180.0),
    FixedPlacement("R511", "F", 87.15, 51.4, 180.0),
    FixedPlacement("R512", "F", 82.2, 47.8, 270.0),
    FixedPlacement("R513", "F", 103.1, 42.55, 90.0),
    # C510/C511 are local at the edge.  The bulk C512 is now a smaller 1206 and uses
    # the solid L3 rail as lower-frequency write-current storage.
    FixedPlacement("C512", "B", 85.25, 29.0),
    FixedPlacement("C510", "F", 104.3, 34.47, 90.0, False, True, False),
    FixedPlacement("C511", "F", 104.3, 30.8, 90.0, False, True, False),
)


TOP_RIGHT_FRONT = Region("F", Box(59.7, 20.9, 82.8, 32.0))
NFC_FRONT = Region("F", Box(59.7, 32.0, 82.8, 46.0))
MCU_FRONT_SIDE = Region("F", Box(74.5, 29.0, 83.0, 46.0))
MCU_FRONT_BOTTOM = Region("F", Box(71.0, 46.0, 97.0, 52.4))
LOWER_FRONT_LEFT = Region("F", Box(24.8, 53.0, 61.0, 66.8))
LOWER_FRONT_RIGHT = Region("F", Box(62.0, 52.8, 97.0, 70.3))
SD_BACK = Region("B", Box(21.0, 52.8, 61.0, 66.8))
SUBGHZ_BACK = Region("B", Box(60.0, 47.0, 83.0, 52.4))
POWER_BUCK_BACK = Region("B", Box(60.0, 28.8, 82.8, 40.2))
POWER_BOOST_BACK = Region("B", Box(60.0, 39.8, 82.8, 52.4))
POWER_RIGHT_BACK = Region("B", Box(83.3, 46.0, 104.7, 72.8))
POWER_MID_BACK = Region("B", Box(60.0, 28.8, 82.8, 52.4))
RIGHT_BACK_TOP = Region("B", Box(59.7, 20.8, 82.8, 52.4))
BACK_UNDER_ESP_BODY = Region("B", Box(84.0, 28.8, 104.7, 45.4))
TOP_FRONT_STRIP = Region("F", Box(24.0, 20.8, 58.8, 23.4))
TOP_BACK_STRIP = Region("B", Box(24.0, 20.8, 58.8, 23.4))


ALLOWED_EXTRA_PADS = {
    "J1": {"A8", "B8"},  # USB2-only symbol intentionally omits SBU1/SBU2.
    "J4": {"MP"},  # JST shell/mounting pads are mechanical.
    "SW5": {"SH"},  # Switch shell tabs are mechanical anchors.
    "U4": {"11"},  # HTRC110 pin 11 is physical but internally not connected.
}


def vec_mm(x_mm: float, y_mm: float) -> pcbnew.VECTOR2I:
    return pcbnew.VECTOR2I_MM(x_mm, y_mm)


def footprint_root() -> Path:
    return Path(sys.executable).resolve().parent.parent / "share" / "kicad" / "footprints"


def natural_key(value: str) -> list[object]:
    return [int(token) if token.isdigit() else token for token in re.split(r"(\d+)", value)]


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


def load_design(path: Path) -> list[dict[str, object]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("format") != 1 or not isinstance(payload.get("parts"), list):
        raise RuntimeError(f"Unsupported or invalid design netlist: {path}")
    parts = payload["parts"]
    references = [str(part["reference"]) for part in parts]
    duplicates = sorted({ref for ref in references if references.count(ref) > 1})
    if duplicates:
        raise RuntimeError("Duplicate references in design netlist: " + ", ".join(duplicates))
    return parts


def load_footprint(hardware_dir: Path, part: dict[str, object]) -> pcbnew.FOOTPRINT:
    footprint_id = str(part.get("footprint", ""))
    if not footprint_id:
        raise RuntimeError(f"{part['reference']} has no footprint")
    try:
        library, name = footprint_id.split(":", 1)
    except ValueError as exc:
        raise RuntimeError(f"Invalid footprint ID for {part['reference']}: {footprint_id}") from exc

    if library == "PocketLab_Custom":
        library_dir = hardware_dir / "PocketLab_Custom.pretty"
    elif library == "PocketLab_Card":
        library_dir = hardware_dir / "footprints" / "PocketLab_Card.pretty"
    else:
        library_dir = footprint_root() / f"{library}.pretty"
    try:
        footprint = pcbnew.FootprintLoad(str(library_dir), name)
    except Exception as exc:
        raise RuntimeError(
            f"KiCad failed loading footprint for {part['reference']}: {footprint_id} "
            f"from {library_dir}"
        ) from exc
    if footprint is None:
        raise RuntimeError(f"Footprint not found for {part['reference']}: {footprint_id}")

    footprint.SetFPID(pcbnew.LIB_ID(library, name))
    footprint.SetReference(str(part["reference"]))
    footprint.SetValue(str(part["value"]))
    footprint.Value().SetVisible(False)
    fields = {str(name): str(value) for name, value in dict(part.get("fields", {})).items()}
    footprint.SetFields(fields)
    for field_name in fields:
        footprint.GetField(field_name).SetVisible(False)
    footprint.SetDNP(bool(part.get("dnp", field_is_true(fields.get("DNP", "")))))
    footprint.SetExcludedFromBOM(not bool(part.get("in_bom", True)))
    return footprint


def validate_footprint_pads(part: dict[str, object], footprint: pcbnew.FOOTPRINT) -> None:
    reference = str(part["reference"])
    schematic_pins = set(str(pin) for pin in dict(part["pins"]))
    footprint_pads = {pad.GetNumber() for pad in footprint.Pads() if pad.GetNumber()}

    missing = sorted(schematic_pins - footprint_pads, key=natural_key)
    if missing:
        raise RuntimeError(
            f"{reference}: schematic pins missing from footprint: {', '.join(missing)}"
        )

    unexpected = footprint_pads - schematic_pins - ALLOWED_EXTRA_PADS.get(reference, set())
    if unexpected:
        raise RuntimeError(
            f"{reference}: unexpected numbered footprint pads: "
            + ", ".join(sorted(unexpected, key=natural_key))
        )


def create_nets(board: pcbnew.BOARD, parts: Sequence[dict[str, object]]) -> dict[str, pcbnew.NETINFO_ITEM]:
    names = sorted(
        {
            pin_board_net_name(part, pin, net_name)
            for part in parts
            for pin, net_name in dict(part["pins"]).items()
        }
    )
    result: dict[str, pcbnew.NETINFO_ITEM] = {}
    for name in names:
        net = pcbnew.NETINFO_ITEM(board, name)
        board.Add(net)
        result[name] = net
    return result


def assign_pad_nets(
    part: dict[str, object], footprint: pcbnew.FOOTPRINT, nets: dict[str, pcbnew.NETINFO_ITEM]
) -> None:
    by_number: dict[str, list[pcbnew.PAD]] = {}
    for pad in footprint.Pads():
        by_number.setdefault(pad.GetNumber(), []).append(pad)

    for pin, net_name_value in dict(part["pins"]).items():
        net_name = pin_board_net_name(part, pin, net_name_value)
        for pad in by_number[str(pin)]:
            pad.SetNet(nets[net_name])


def reset_to_front(footprint: pcbnew.FOOTPRINT) -> None:
    if footprint.GetLayer() == pcbnew.B_Cu:
        footprint.Flip(footprint.GetPosition(), pcbnew.FLIP_DIRECTION_LEFT_RIGHT)
    footprint.SetOrientationDegrees(0.0)
    footprint.SetPosition(vec_mm(0.0, 0.0))


def configure_side_and_rotation(
    footprint: pcbnew.FOOTPRINT, side: str, rotation_deg: float
) -> None:
    reset_to_front(footprint)
    footprint.SetOrientationDegrees(rotation_deg)
    if side == "B":
        footprint.Flip(footprint.GetPosition(), pcbnew.FLIP_DIRECTION_LEFT_RIGHT)
    if side not in {"F", "B"}:
        raise ValueError(f"Invalid side {side!r}")


def courtyard_box(footprint: pcbnew.FOOTPRINT, side: str) -> Box:
    footprint.BuildCourtyardCaches()
    layer = pcbnew.F_CrtYd if side == "F" else pcbnew.B_CrtYd
    bounds = footprint.GetCourtyard(layer).BBox()
    if bounds.GetWidth() <= 0 or bounds.GetHeight() <= 0:
        bounds = footprint.GetBoundingBox(False, False)
    return Box(
        pcbnew.ToMM(bounds.GetLeft()),
        pcbnew.ToMM(bounds.GetTop()),
        pcbnew.ToMM(bounds.GetRight()),
        pcbnew.ToMM(bounds.GetBottom()),
    )


def center_footprint(
    footprint: pcbnew.FOOTPRINT, side: str, x_mm: float, y_mm: float, rotation_deg: float
) -> Box:
    configure_side_and_rotation(footprint, side, rotation_deg)
    footprint.SetPosition(vec_mm(x_mm, y_mm))
    initial = courtyard_box(footprint, side)
    delta_x = x_mm - (initial.left + initial.right) / 2.0
    delta_y = y_mm - (initial.top + initial.bottom) / 2.0
    footprint.Move(vec_mm(delta_x, delta_y))
    return courtyard_box(footprint, side)


def place_at_origin(
    footprint: pcbnew.FOOTPRINT, side: str, x_mm: float, y_mm: float, rotation_deg: float
) -> Box:
    configure_side_and_rotation(footprint, side, rotation_deg)
    footprint.SetPosition(vec_mm(x_mm, y_mm))
    return courtyard_box(footprint, side)


def set_reference_style(footprint: pcbnew.FOOTPRINT, side: str) -> None:
    """Keep dense reference designators on assembly/Fab, not production silk."""
    bounds = courtyard_box(footprint, side)
    reference = footprint.Reference()
    reference.SetVisible(True)
    reference.SetLayer(pcbnew.F_Fab if side == "F" else pcbnew.B_Fab)
    reference.SetTextSize(vec_mm(0.8, 0.8))
    reference.SetTextThickness(pcbnew.FromMM(0.12))
    reference.SetPosition(vec_mm((bounds.left + bounds.right) / 2.0, bounds.top - 0.38))


def move_silk_graphics_to_fab(
    footprint: pcbnew.FOOTPRINT, *, text_only: bool = False
) -> None:
    """Move reviewed, hidden-by-part silk detail onto the assembly layer."""
    back = footprint.GetLayer() == pcbnew.B_Cu
    silk_layer = pcbnew.B_SilkS if back else pcbnew.F_SilkS
    fab_layer = pcbnew.B_Fab if back else pcbnew.F_Fab
    text_types = (pcbnew.PCB_TEXT, pcbnew.PCB_TEXTBOX)
    for item in footprint.GraphicalItems():
        if item.GetLayer() != silk_layer:
            continue
        if text_only and not isinstance(item, text_types):
            continue
        item.SetLayer(fab_layer)


def clean_reviewed_silkscreen(footprints: dict[str, pcbnew.FOOTPRINT]) -> None:
    # These outlines are under fitted modules/connectors; their Fab geometry
    # remains intact, while the board-level guides retain antenna/service data.
    # Moving them prevents pad clipping and rounded-edge violations in output
    # silkscreen without weakening the DRC rules.
    for reference in ("U1", "U3", "J1", "J4", "J8", "U19"):
        move_silk_graphics_to_fab(footprints[reference])
    # The stock LED pin-number text extends into the neighbouring 6-mm-pitch
    # LED outline. Keep the orientation mark in Fab and retain the silk body.
    for reference in ("LED1", "LED2", "LED3", "LED4"):
        move_silk_graphics_to_fab(footprints[reference], text_only=True)


def normal_board_box() -> Box:
    return Box(
        BOARD_LEFT + NORMAL_INSET,
        BOARD_TOP + NORMAL_INSET,
        BOARD_RIGHT - NORMAL_INSET,
        BOARD_BOTTOM - NORMAL_INSET,
    )


def box_intersects_circle(box: Box, x_mm: float, y_mm: float, radius_mm: float) -> bool:
    closest_x = min(max(x_mm, box.left), box.right)
    closest_y = min(max(y_mm, box.top), box.bottom)
    return (closest_x - x_mm) ** 2 + (closest_y - y_mm) ** 2 <= radius_mm**2


def violates_reserved_area(reference: str, side: str, box: Box) -> str | None:
    if reference != "U1" and box.expanded(PLACEMENT_CLEARANCE).intersects(ESP_ANTENNA):
        return "ESP32 antenna keepout"
    if reference != "AE1" and box.expanded(PLACEMENT_CLEARANCE).intersects(NFC_OUTER):
        return "complete NFC loop component keepout"
    if reference != "A1" and box.expanded(PLACEMENT_CLEARANCE).intersects(
        SUBGHZ_ANTENNA_RESERVE
    ):
        return "Sub-GHz spring antenna anchor/notch keepout"
    if (
        side == "F"
        and reference not in LF_SUPPORT_REFERENCES
        and box.expanded(PLACEMENT_CLEARANCE).intersects(LF_SUPPORT_ROUTING_RESERVE)
    ):
        return "LF-RFID front-side power-routing reserve"
    return None


def collision_with(
    reference: str, side: str, box: Box, occupied: Sequence[Occupied]
) -> Occupied | None:
    candidate = box.expanded(PLACEMENT_CLEARANCE)
    for item in occupied:
        # J2 intentionally spans the B-side projection of U1's embedded pad-41
        # thermal vias.  Those 0.20 mm holes omit B.Mask and are fully tented;
        # the nearest J2 shell copper starts 0.40 mm beyond the shifted via
        # array.  Retain every U1 via as an obstacle for all other footprints.
        reviewed_tented_via_overlap = (
            reference == "J2" and item.reference == "U1.PTH41"
        )
        if (
            item.side == side
            and not reviewed_tented_via_overlap
            and candidate.intersects(item.box)
        ):
            return item
    return None


def register_placement(
    occupied: list[Occupied],
    reference: str,
    side: str,
    box: Box,
    blocks_both_sides: bool = False,
) -> None:
    occupied.append(Occupied(reference, side, box))
    if blocks_both_sides:
        occupied.append(Occupied(reference, "B" if side == "F" else "F", box))


def through_hole_boxes(footprint: pcbnew.FOOTPRINT) -> list[tuple[str, Box]]:
    result: list[tuple[str, Box]] = []
    for index, pad in enumerate(footprint.Pads()):
        if pad.GetAttribute() != pcbnew.PAD_ATTRIB_PTH:
            continue
        bounds = pad.GetBoundingBox()
        result.append(
            (
                pad.GetNumber() or str(index),
                Box(
                    pcbnew.ToMM(bounds.GetLeft()),
                    pcbnew.ToMM(bounds.GetTop()),
                    pcbnew.ToMM(bounds.GetRight()),
                    pcbnew.ToMM(bounds.GetBottom()),
                ),
            )
        )
    return result


def pth_collision(
    footprint: pcbnew.FOOTPRINT,
    reference: str,
    side: str,
    occupied: Sequence[Occupied],
) -> tuple[str, Occupied] | None:
    opposite = "B" if side == "F" else "F"
    for pad_number, box in through_hole_boxes(footprint):
        collision = collision_with(reference, opposite, box, occupied)
        if collision:
            return pad_number, collision
    return None


def register_pth_obstacles(
    occupied: list[Occupied], footprint: pcbnew.FOOTPRINT, reference: str, side: str
) -> None:
    opposite = "B" if side == "F" else "F"
    for pad_number, box in through_hole_boxes(footprint):
        occupied.append(Occupied(f"{reference}.PTH{pad_number}", opposite, box))


def place_fixed(
    footprint: pcbnew.FOOTPRINT,
    placement: FixedPlacement,
    occupied: list[Occupied],
) -> Box:
    if placement.reference == "U1":
        place_at_origin(
            footprint,
            placement.side,
            placement.x_mm,
            placement.y_mm,
            placement.rotation_deg,
        )
        box = ESP_BODY
    elif placement.use_origin:
        box = place_at_origin(
            footprint,
            placement.side,
            placement.x_mm,
            placement.y_mm,
            placement.rotation_deg,
        )
    else:
        box = center_footprint(
            footprint,
            placement.side,
            placement.x_mm,
            placement.y_mm,
            placement.rotation_deg,
        )

    reserved = violates_reserved_area(placement.reference, placement.side, box)
    if reserved:
        raise RuntimeError(f"Fixed placement {placement.reference} intersects {reserved}: {box}")
    if not placement.allow_edge and not box.inside(normal_board_box()):
        raise RuntimeError(f"Fixed placement {placement.reference} crosses board inset: {box}")
    collision = collision_with(placement.reference, placement.side, box, occupied)
    if collision:
        raise RuntimeError(
            f"Fixed placement {placement.reference} overlaps {collision.reference} on "
            f"{placement.side}: {box} vs {collision.box}"
        )
    opposite = "B" if placement.side == "F" else "F"
    if placement.blocks_both_sides:
        opposite_collision = collision_with(placement.reference, opposite, box, occupied)
        if opposite_collision:
            raise RuntimeError(
                f"Fixed placement {placement.reference} volume overlaps "
                f"{opposite_collision.reference} on {opposite}"
            )
    else:
        pad_collision = pth_collision(
            footprint, placement.reference, placement.side, occupied
        )
        if pad_collision:
            pad_number, item = pad_collision
            raise RuntimeError(
                f"Fixed placement {placement.reference} PTH {pad_number} overlaps "
                f"{item.reference} on {opposite}"
            )
    register_placement(
        occupied,
        placement.reference,
        placement.side,
        box,
        placement.blocks_both_sides,
    )
    if not placement.blocks_both_sides:
        register_pth_obstacles(
            occupied, footprint, placement.reference, placement.side
        )
    set_reference_style(footprint, placement.side)
    return box


def candidate_centers(region: Box, width: float, height: float) -> Iterable[tuple[float, float]]:
    x_start = region.left + width / 2.0
    x_stop = region.right - width / 2.0
    y_start = region.top + height / 2.0
    y_stop = region.bottom - height / 2.0
    if x_start > x_stop or y_start > y_stop:
        return
    step = 0.5
    y = y_start
    while y <= y_stop + 1e-6:
        x = x_start
        while x <= x_stop + 1e-6:
            yield round(x, 3), round(y, 3)
            x += step
        y += step


def place_in_regions(
    footprint: pcbnew.FOOTPRINT,
    reference: str,
    regions: Sequence[Region],
    occupied: list[Occupied],
) -> tuple[str, Box]:
    for region in regions:
        for rotation in (0.0, 90.0):
            probe = center_footprint(footprint, region.side, 0.0, 0.0, rotation)
            for x_mm, y_mm in candidate_centers(region.box, probe.width, probe.height):
                box = center_footprint(footprint, region.side, x_mm, y_mm, rotation)
                if not box.inside(region.box) or not box.inside(normal_board_box()):
                    continue
                if violates_reserved_area(reference, region.side, box):
                    continue
                if collision_with(reference, region.side, box, occupied):
                    continue
                if pth_collision(footprint, reference, region.side, occupied):
                    continue
                register_placement(occupied, reference, region.side, box)
                register_pth_obstacles(occupied, footprint, reference, region.side)
                set_reference_style(footprint, region.side)
                return region.side, box
    raise RuntimeError(
        f"No collision-free placement found for {reference} in "
        + ", ".join(f"{region.side}:{region.box}" for region in regions)
    )


def refs(*values: str) -> set[str]:
    return set(values)


POWER_USB = refs("R101", "R102", "R103", "C101", "F1", "D101", "U16")
POWER_PROTECTION = refs(
    "R104", "C102", "R105", "R106", "R107", "TP101", "TP102", "TP104"
)
POWER_CHARGER = refs(
    "R108", "R109", "R110", "R111", "R112", "R113", "R114", "R115", "R126", "R128",
    "C103", "C104", "C105", "C106", "C121", "C122",
)
POWER_BUCK = refs(
    "R116", "R117", "R118", "R119", "C107", "C108", "C109", "C110", "C111", "C112",
    "C123", "C124",
)
POWER_BOOST = refs(
    "R120", "R121", "R122", "R129", "C113", "C114", "C115", "C116"
)
POWER_AUX = refs("R123", "R124", "R127", "C117", "C118", "C119")
POWER_GAUGE = refs("R125", "C120")

SD_REFS = refs(
    "R510", "R511", "R512", "R513", "R514", "R515", "R516", "R517", "R518", "R519",
    "C510", "C511",
)
EXPANSION_REFS = {f"R{number}" for number in range(710, 735)}
POWER_TESTPOINTS = {f"TP{number}" for number in range(101, 111)}


def region_plan(part: dict[str, object]) -> Sequence[Region]:
    reference = str(part["reference"])
    block = str(part["block"])

    if reference in POWER_TESTPOINTS:
        return (
            LOWER_FRONT_LEFT,
            LOWER_FRONT_RIGHT,
            TOP_FRONT_STRIP,
            MCU_FRONT_BOTTOM,
            RIGHT_BACK_TOP,
            SD_BACK,
            POWER_RIGHT_BACK,
            POWER_MID_BACK,
            BACK_UNDER_ESP_BODY,
            TOP_BACK_STRIP,
        )
    if reference in POWER_USB:
        return (POWER_RIGHT_BACK, POWER_MID_BACK, BACK_UNDER_ESP_BODY, SD_BACK, TOP_BACK_STRIP)
    if reference in POWER_PROTECTION:
        return (POWER_RIGHT_BACK, POWER_MID_BACK, BACK_UNDER_ESP_BODY, SD_BACK, TOP_BACK_STRIP)
    if reference in POWER_CHARGER:
        return (POWER_RIGHT_BACK, POWER_MID_BACK, BACK_UNDER_ESP_BODY, SD_BACK, TOP_BACK_STRIP)
    if reference in POWER_BUCK:
        return (POWER_BUCK_BACK, POWER_MID_BACK, POWER_RIGHT_BACK, BACK_UNDER_ESP_BODY, SD_BACK, TOP_BACK_STRIP)
    if reference in POWER_BOOST:
        return (POWER_BOOST_BACK, POWER_MID_BACK, POWER_RIGHT_BACK, BACK_UNDER_ESP_BODY, SD_BACK, TOP_BACK_STRIP)
    if reference in POWER_AUX or reference in POWER_GAUGE:
        return (POWER_RIGHT_BACK, POWER_MID_BACK, BACK_UNDER_ESP_BODY, SD_BACK, TOP_BACK_STRIP)
    if block.startswith("01 "):
        return (POWER_RIGHT_BACK, POWER_MID_BACK, BACK_UNDER_ESP_BODY, SD_BACK, TOP_BACK_STRIP)
    if block.startswith("02 "):
        return (
            MCU_FRONT_SIDE,
            MCU_FRONT_BOTTOM,
            NFC_FRONT,
            LOWER_FRONT_LEFT,
            TOP_FRONT_STRIP,
            BACK_UNDER_ESP_BODY,
        )
    if block.startswith("03 "):
        return (NFC_FRONT, MCU_FRONT_BOTTOM, TOP_RIGHT_FRONT, LOWER_FRONT_RIGHT)
    if block.startswith("04 "):
        return (SUBGHZ_BACK, POWER_BOOST_BACK, MCU_FRONT_BOTTOM, LOWER_FRONT_RIGHT)
    if block.startswith("05 ") and reference in SD_REFS:
        return (SD_BACK, LOWER_FRONT_LEFT)
    if block.startswith("05 "):
        return (
            TOP_FRONT_STRIP,
            TOP_RIGHT_FRONT,
            NFC_FRONT,
            LOWER_FRONT_LEFT,
            SD_BACK,
            RIGHT_BACK_TOP,
            BACK_UNDER_ESP_BODY,
            POWER_RIGHT_BACK,
        )
    if block.startswith("06 "):
        return (
            LOWER_FRONT_LEFT,
            LOWER_FRONT_RIGHT,
            NFC_FRONT,
            TOP_RIGHT_FRONT,
            MCU_FRONT_BOTTOM,
            SD_BACK,
            RIGHT_BACK_TOP,
            BACK_UNDER_ESP_BODY,
            POWER_RIGHT_BACK,
            POWER_MID_BACK,
            TOP_FRONT_STRIP,
            TOP_BACK_STRIP,
        )
    if block.startswith("07 ") and reference in EXPANSION_REFS:
        return (
            LOWER_FRONT_LEFT,
            LOWER_FRONT_RIGHT,
            SD_BACK,
            TOP_FRONT_STRIP,
            TOP_BACK_STRIP,
            NFC_FRONT,
            MCU_FRONT_BOTTOM,
            RIGHT_BACK_TOP,
            BACK_UNDER_ESP_BODY,
            POWER_RIGHT_BACK,
        )
    if block.startswith("07 "):
        return (RIGHT_BACK_TOP, SD_BACK, POWER_MID_BACK, POWER_RIGHT_BACK)
    raise RuntimeError(f"No placement region plan for {reference} ({block})")


def footprint_area(footprint: pcbnew.FOOTPRINT) -> float:
    configure_side_and_rotation(footprint, "F", 0.0)
    box = courtyard_box(footprint, "F")
    return box.width * box.height


def placement_priority(part: dict[str, object]) -> int:
    """Pack the small, topology-constrained RF/edge regions before power."""
    block = str(part["block"])
    if block.startswith("04 "):
        return 0
    if block.startswith("05 "):
        return 1
    if block.startswith("03 "):
        return 2
    if block.startswith("02 "):
        return 3
    if block.startswith("07 ") and str(part["reference"]) in EXPANSION_REFS:
        return 4
    if block.startswith("07 "):
        return 5
    if block.startswith("01 "):
        return 6
    if block.startswith("06 "):
        return 7
    return 8


def add_rect(board: pcbnew.BOARD, box: Box, layer: int, width_mm: float = 0.18) -> None:
    shape = pcbnew.PCB_SHAPE(board)
    shape.SetShape(pcbnew.SHAPE_T_RECT)
    shape.SetStart(vec_mm(box.left, box.top))
    shape.SetEnd(vec_mm(box.right, box.bottom))
    shape.SetLayer(layer)
    shape.SetWidth(pcbnew.FromMM(width_mm))
    board.Add(shape)


def add_circle(
    board: pcbnew.BOARD,
    x_mm: float,
    y_mm: float,
    radius_mm: float,
    layer: int,
    width_mm: float = 0.18,
) -> None:
    shape = pcbnew.PCB_SHAPE(board)
    shape.SetShape(pcbnew.SHAPE_T_CIRCLE)
    shape.SetCenter(vec_mm(x_mm, y_mm))
    shape.SetEnd(vec_mm(x_mm + radius_mm, y_mm))
    shape.SetLayer(layer)
    shape.SetWidth(pcbnew.FromMM(width_mm))
    board.Add(shape)


def add_text(
    board: pcbnew.BOARD,
    value: str,
    x_mm: float,
    y_mm: float,
    layer: int,
    size_mm: float = 0.75,
    mirrored: bool = False,
) -> None:
    item = pcbnew.PCB_TEXT(board)
    item.SetText(value)
    item.SetPosition(vec_mm(x_mm, y_mm))
    item.SetLayer(layer)
    item.SetTextSize(vec_mm(size_mm, size_mm))
    item.SetTextThickness(pcbnew.FromMM(max(0.1, size_mm * 0.14)))
    item.SetHorizJustify(pcbnew.GR_TEXT_H_ALIGN_CENTER)
    item.SetMirrored(mirrored)
    board.Add(item)


def add_placement_guides(board: pcbnew.BOARD) -> None:
    add_rect(board, NFC_OUTER, pcbnew.Dwgs_User)
    add_text(board, "AE1 FULL COMPONENT KEEPOUT - BOTH SIDES", 40.9, 51.4, pcbnew.Cmts_User)
    add_rect(board, ESP_ANTENNA, pcbnew.Dwgs_User)
    add_text(board, "ESP32 ANTENNA KEEPOUT", 93.0, 27.4, pcbnew.Cmts_User, 0.65)
    add_rect(board, SUBGHZ_ANTENNA_RESERVE, pcbnew.Dwgs_User)
    add_text(board, "868MHz SPRING INSIDE POCKET", 76.2, 66.9, pcbnew.Cmts_User, 0.55)
    add_text(
        board,
        "NETLISTED PLACEMENT ONLY - UNROUTED / NOT FOR PRODUCTION",
        70.5,
        72.7,
        pcbnew.Cmts_User,
        0.72,
    )
    # Reset and boot are adjacent front-side service controls. Keep their
    # function readable on both production silk and assembly documentation.
    for layer in (pcbnew.F_SilkS, pcbnew.F_Fab):
        add_text(board, "RESET", 72.0, 45.2, layer, 0.80)
        add_text(board, "BOOT", 78.0, 45.2, layer, 0.80)
        add_text(board, "UP", 51.0, 51.75, layer, 0.80)
        add_text(board, "OK", 57.0, 51.75, layer, 0.80)
        add_text(board, "DOWN", 63.0, 51.75, layer, 0.80)

    # JST-PH cable polarity is not standardized. J4 now sits on the back, so
    # its board-defined polarity labels are mirrored on back silk and fab.
    for layer in (pcbnew.B_SilkS, pcbnew.B_Fab):
        add_text(board, "+", 101.1, 68.2, layer, 0.80, True)
        add_text(board, "-", 103.1, 68.2, layer, 0.80, True)


def remove_template_caption(board: pcbnew.BOARD) -> None:
    for drawing in list(board.GetDrawings()):
        if isinstance(drawing, pcbnew.PCB_TEXT) and "MECHANICAL SCAFFOLD" in drawing.GetText():
            board.Remove(drawing)


def add_subghz_antenna_notch(board: pcbnew.BOARD) -> None:
    """Replace the lower straight edge with a shallow open antenna notch."""
    bottom_segment = None
    for drawing in list(board.GetDrawings()):
        if drawing.GetLayer() != pcbnew.Edge_Cuts or not isinstance(drawing, pcbnew.PCB_SHAPE):
            continue
        start = drawing.GetStart()
        end = drawing.GetEnd()
        if (
            abs(pcbnew.ToMM(start.y) - BOARD_BOTTOM) < 0.001
            and abs(pcbnew.ToMM(end.y) - BOARD_BOTTOM) < 0.001
            and {round(pcbnew.ToMM(start.x), 2), round(pcbnew.ToMM(end.x), 2)}
            == {23.18, 102.42}
        ):
            bottom_segment = drawing
            break
    if bottom_segment is None:
        raise RuntimeError("Cannot find the mechanical template's lower straight Edge.Cuts segment")
    width = bottom_segment.GetWidth()
    board.Remove(bottom_segment)
    points = (
        (23.18, BOARD_BOTTOM),
        (SUBGHZ_NOTCH_LEFT, BOARD_BOTTOM),
        (SUBGHZ_NOTCH_LEFT, SUBGHZ_NOTCH_TOP),
        (SUBGHZ_NOTCH_RIGHT, SUBGHZ_NOTCH_TOP),
        (SUBGHZ_NOTCH_RIGHT, BOARD_BOTTOM),
        (102.42, BOARD_BOTTOM),
    )
    for (x1, y1), (x2, y2) in zip(points, points[1:]):
        segment = pcbnew.PCB_SHAPE(board)
        segment.SetShape(pcbnew.SHAPE_T_SEGMENT)
        segment.SetStart(vec_mm(x1, y1))
        segment.SetEnd(vec_mm(x2, y2))
        segment.SetLayer(pcbnew.Edge_Cuts)
        segment.SetWidth(width)
        board.Add(segment)


def validate_round_trip(
    output: Path,
    expected_parts: Sequence[dict[str, object]],
    expected_net_names: set[str],
) -> None:
    board = pcbnew.LoadBoard(str(output))
    footprints = list(board.GetFootprints())
    actual_refs = {footprint.GetReference() for footprint in footprints}
    expected_refs = {str(part["reference"]) for part in expected_parts}
    if actual_refs != expected_refs:
        missing = sorted(expected_refs - actual_refs, key=natural_key)
        extra = sorted(actual_refs - expected_refs, key=natural_key)
        raise RuntimeError(f"Round-trip reference mismatch; missing={missing}, extra={extra}")

    actual_nets = {
        pad.GetNetname()
        for footprint in footprints
        for pad in footprint.Pads()
        if pad.GetNetname()
    }
    if actual_nets != expected_net_names:
        raise RuntimeError(
            "Round-trip net mismatch; missing="
            + repr(sorted(expected_net_names - actual_nets))
            + ", extra="
            + repr(sorted(actual_nets - expected_net_names))
        )

    expected_assignments = {
        (
            str(part["reference"]),
            str(pin),
            pin_board_net_name(part, pin, net_name),
        )
        for part in expected_parts
        for pin, net_name in dict(part["pins"]).items()
    }
    actual_assignments = {
        (footprint.GetReference(), pad.GetNumber(), pad.GetNetname())
        for footprint in footprints
        for pad in footprint.Pads()
        if pad.GetNumber() and pad.GetNetname()
    }
    missing_assignments = expected_assignments - actual_assignments
    if missing_assignments:
        sample = sorted(missing_assignments, key=lambda row: (natural_key(row[0]), natural_key(row[1])))[:12]
        raise RuntimeError(f"Round-trip lost pad/net assignments, first entries: {sample}")

    actual_by_ref = {footprint.GetReference(): footprint for footprint in footprints}
    metadata_mismatches: list[str] = []
    for part in expected_parts:
        reference = str(part["reference"])
        footprint = actual_by_ref[reference]
        fields = {
            str(name): str(value)
            for name, value in dict(part.get("fields", {})).items()
        }
        expected_dnp = bool(
            part.get("dnp", field_is_true(fields.get("DNP", "")))
        )
        expected_in_bom = bool(part.get("in_bom", True))
        if bool(footprint.IsDNP()) != expected_dnp:
            metadata_mismatches.append(f"{reference}:DNP")
        if bool(footprint.IsExcludedFromBOM()) == expected_in_bom:
            metadata_mismatches.append(f"{reference}:in_bom")
        for field_name, field_value in fields.items():
            if not footprint.HasField(field_name):
                metadata_mismatches.append(f"{reference}:missing {field_name}")
            elif footprint.GetFieldText(field_name) != field_value:
                metadata_mismatches.append(f"{reference}:{field_name}")
    if metadata_mismatches:
        raise RuntimeError(
            "Round-trip footprint metadata mismatch, first entries: "
            + ", ".join(metadata_mismatches[:12])
        )


def validate_serialized_stackup(output: Path) -> None:
    serialized = output.read_text(encoding="utf-8")
    required_fragments = (
        "(stackup",
        "(thickness 1.2)",
        '(layer "F.Cu"',
        '(layer "In1.Cu"',
        '(layer "In2.Cu"',
        '(layer "B.Cu"',
        "(thickness 0.035)",
        "(thickness 0.0152)",
        "(thickness 0.2104)",
        "(thickness 0.665)",
        '(material "7628")',
        "(epsilon_r 4.4)",
        '(copper_finish "ENIG")',
        '(4 "In1.Cu" power "GND")',
        '(6 "In2.Cu" power "PWR")',
    )
    missing = [fragment for fragment in required_fragments if fragment not in serialized]
    if missing:
        raise RuntimeError("Serialized JLC04121H-7628 stackup is incomplete: " + repr(missing))


def main() -> int:
    parser = argparse.ArgumentParser()
    hardware_dir = Path(__file__).resolve().parent.parent
    parser.add_argument(
        "--design",
        type=Path,
        default=hardware_dir / "design-netlist.json",
        help="JSON emitted by generate_schematic.py",
    )
    parser.add_argument(
        "--template",
        type=Path,
        default=hardware_dir / "templates" / "PocketLab-Card-mechanical.kicad_pcb",
        help="empty PCB containing the exact mechanical outline",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=hardware_dir / "PocketLab-Card-netlisted.kicad_pcb",
        help="staging PCB output; the main board is protected",
    )
    args = parser.parse_args()

    design_path = args.design.resolve()
    template_path = args.template.resolve()
    output_path = args.output.resolve()
    protected_main = (hardware_dir / "PocketLab-Card.kicad_pcb").resolve()
    if output_path == protected_main:
        raise RuntimeError("Refusing to overwrite hardware/PocketLab-Card.kicad_pcb")

    all_parts = load_design(design_path)
    populated_parts = [part for part in all_parts if str(part.get("footprint", ""))]
    if len(all_parts) != 274 or len(populated_parts) != 267:
        raise RuntimeError(
            f"Design count changed: expected 274 symbols / 267 footprints, got "
            f"{len(all_parts)} / {len(populated_parts)}. Review placement assumptions first."
        )

    board = pcbnew.LoadBoard(str(template_path))
    if list(board.GetFootprints()):
        raise RuntimeError("Mechanical template is not empty")
    # KiCad 10.0.5's bundled SWIG bindings intermittently expose the title
    # block as a raw pointer when a board is loaded from a resolved Path.  The
    # mechanical template already carries the authoritative card title, so we
    # leave that metadata intact and place the staging warning on Cmts.User.

    nets = create_nets(board, populated_parts)
    footprints: dict[str, pcbnew.FOOTPRINT] = {}
    parts_by_ref = {str(part["reference"]): part for part in populated_parts}
    internally_commoned_footprints = {
        "J1",
        "J2",
    }
    for part in populated_parts:
        footprint = load_footprint(hardware_dir, part)
        validate_footprint_pads(part, footprint)
        assign_pad_nets(part, footprint, nets)
        reference = str(part["reference"])
        if reference == "U7":
            # The manufacturer's 0.5-mm-pitch SOT-563 land pattern has a
            # native 0.15-mm copper gap. Store the bounded override on the
            # lands themselves so CLI DRC and regenerated boards agree even
            # when broader project rules are reordered.
            for pad in footprint.Pads():
                pad.SetLocalClearance(pcbnew.FromMM(0.15))
        if str(part["reference"]) in internally_commoned_footprints:
            # The USB-C and microSD connector footprints duplicate their
            # continuous metal-shell contact. Identically numbered terminals
            # are internally commoned by the physical part in both cases.
            footprint.SetDuplicatePadNumbersAreJumpers(True)
        footprints[str(part["reference"])] = footprint

    # BOARD.Remove invalidates KiCad's short-lived PCB_IO plug-in wrapper in
    # 10.0.5, so template drawings are removed only after every library
    # footprint has been loaded.
    remove_template_caption(board)
    add_subghz_antenna_notch(board)
    # FOOTPRINT.Flip dereferences the parent board in KiCad 10.0.5.  Attach
    # every loaded footprint before any back-side placement to avoid a native
    # access violation in the SWIG binding.
    for footprint in footprints.values():
        board.Add(footprint)

    fixed_by_ref = {placement.reference: placement for placement in FIXED_PLACEMENTS}
    unknown_fixed = sorted(set(fixed_by_ref) - set(footprints), key=natural_key)
    if unknown_fixed:
        raise RuntimeError("Fixed placement references missing from design: " + ", ".join(unknown_fixed))

    occupied: list[Occupied] = []
    # Keep the RF antenna volume clear on the back even before U1 is registered.
    occupied.append(Occupied("U1 ANTENNA KEEPOUT", "B", ESP_ANTENNA))

    for placement in FIXED_PLACEMENTS:
        footprint = footprints[placement.reference]
        place_fixed(footprint, placement, occupied)

    remaining = [
        part for part in populated_parts if str(part["reference"]) not in fixed_by_ref
    ]
    remaining.sort(
        key=lambda part: (
            placement_priority(part),
            -footprint_area(footprints[str(part["reference"])]),
            natural_key(str(part["reference"])),
        )
    )
    side_counts = {"F": 0, "B": 0}
    side_counts.update(
        {
            side: sum(1 for placement in FIXED_PLACEMENTS if placement.side == side)
            for side in ("F", "B")
        }
    )
    for part in remaining:
        reference = str(part["reference"])
        footprint = footprints[reference]
        try:
            side, _ = place_in_regions(footprint, reference, region_plan(part), occupied)
        except RuntimeError:
            debug_path = output_path.with_name("PocketLab-Card-placement-debug.kicad_pcb")
            pcbnew.SaveBoard(str(debug_path), board)
            raise
        side_counts[side] += 1

    clean_reviewed_silkscreen(footprints)
    add_placement_guides(board)
    title_block = board.GetTitleBlock()
    title_block.SetRevision("PROTOTYPE")
    title_block.SetComment(2, "UNROUTED netlisted placement; not for production")
    board.SetTitleBlock(title_block)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    pcbnew.SaveBoard(str(output_path), board)
    # Give KiCad/CLI the same netclasses and custom-rule context as the main
    # project without touching the main PCB itself.
    project_source = hardware_dir / "PocketLab-Card.kicad_pro"
    project_output = output_path.with_suffix(".kicad_pro")
    if project_source.resolve() != project_output.resolve():
        shutil.copyfile(project_source, project_output)
    rules_source = hardware_dir / "PocketLab-Card.kicad_dru"
    if rules_source.exists():
        shutil.copyfile(rules_source, output_path.with_suffix(".kicad_dru"))

    expected_nets = {
        pin_board_net_name(part, pin, net_name)
        for part in populated_parts
        for pin, net_name in dict(part["pins"]).items()
    }
    validate_round_trip(output_path, populated_parts, expected_nets)
    validate_serialized_stackup(output_path)

    print(f"Saved netlisted placement: {output_path}")
    print(
        f"Symbols: {len(all_parts)}; populated footprints: {len(populated_parts)} "
        f"(front {side_counts['F']}, back {side_counts['B']}); nets: {len(expected_nets)}"
    )
    print("Pad audit: no schematic pin is missing; reviewed mechanical/NC extras only")
    print("Stackup audit: JLC04121H-7628 / ENIG serialized; L2=GND and L3=PWR")
    print("Placement audit: no unapproved AABB courtyard, NFC/antenna/service-keepout or board-inset collision")
    print(
        "Known edge exceptions: U1, J1, J4, J5, A1, D1-D3, U13, SW6, Q2 and Q3 "
        "are intentional"
    )
    print("Routing was not generated; run KiCad DRC and RF/power review before using this board")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
