# Work checkpoint — 2026-09-16

## Current authoritative routing state — 2026-09-16

- The authoritative board is `hardware/PocketLab-Card.kicad_pcb`.
- KiCad connectivity reports **33 open connection items**.
- A fresh KiCad 10 DRC run reports **0 geometric/electrical violations** and
  **7 footprint-library comparison notices**.
- The board currently contains 264 footprints, 4,863 track segments, 1,019
  vias and 24 zones.
- Current top, bottom and isometric renders plus an interactive GLB viewer are
  stored under `docs/design-overview/`.
- This is a safe routing checkpoint, not an order-ready fabrication release.

The dated notes below document older routing phases and are retained as design
history; their connection and DRC counts are no longer the current status.

## Routing/DRC continuation — 2026-08-30

- The active board is `hardware/PocketLab-Card-routing-working.kicad_pcb`.
  It contains 2,843 track segments, 575 conventional through-vias and 23
  zones. KiCad connectivity reports 0 open connection items.
- This continuation reassigned ten reviewed long signal segments between the
  two inner copper layers. Every accepted change was tested separately and in
  combination; no track coordinates, footprints, vias or net assignments were
  changed.
- One additional USER_BUTTON_A_N diagonal was replaced by a four-segment,
  obstacle-aware path on the same inner layer. This reduced its local median
  from 20 to 18 findings without adding a via or an open connection.
- The latest continuation rerouted EX4, EX5, CHG_TS_FIXED, GPIO8_MCU and
  SD_DETECT_N, moved AUX5_FAULT_N and BAT_SENSE vias with layer-matched
  fanouts, and reassigned CHARGER_CHG_N from L2 to L3. All accepted edits
  retain 0 open connection items and use only conventional through-vias.
- Three final full-board DRC runs reported 1,162, 1,164 and 1,166 findings.
  The saved median report has 502 clearance, 4 copper-edge, 201 hole-clearance,
  199 shorting, 106 solder-mask bridge and 147 crossing findings. Crossing
  counts are not fully deterministic between
  otherwise identical KiCad CLI runs, so local repeated checks are used when
  accepting a routing-layer change.
- The stale AE1 NFC-loop library rule now matches the intentional board
  keepout and no longer causes a footprint mismatch. The remaining comparison
  findings include project-specific board geometry on U1/U3 plus current
  KiCad standard-library differences; they are documented rather than
  overwritten blindly.
- This is a saved engineering checkpoint, not an order-ready fabrication
  release.

## Active PCB routing state — 2026-08-21

- Work has resumed on the PCB only; firmware and app work remain deferred.
- The 2026-08-16 continuation corrected the physical internal-contact
  semantics of the six KMR2 switches and the continuous J1/J2 shell contacts.
  This removed one false ratsnest item without adding copper. J2 was also
  shifted 0.175 mm inward; all ten microSD copper-to-edge findings are gone
  while the adjacent B.Cu ground track retains exactly 0.20 mm clearance.
- U2.5 and U2.8 now share a short, via-free 0.15-mm NFC_DVDD escape on F.Cu.
  The former long U2.3 ground branch was folded directly into the exposed GND
  pad, one redundant west-side GND via was removed, and the retained plane via
  moved 0.20 mm right / 0.10 mm up into a clearance-clean site. U2.3, U2.7 and U2.41 remain
  explicitly connectivity-checked to GND.
- Current local checkpoint saved on 2026-08-21 Europe/Berlin; the
  authoritative PCB, DRC/ERC reports and progress documentation are in sync.
  No commit or remote push was requested in this continuation.
- LF RFID shares the existing SPI SCK/MOSI/MISO bus. U21 remains the
  partial-power-down-safe SN74LV125ATPWR (LCSC C2675655), and U22 remains the
  LF_RFID_EN-controlled SN74LVC1G126DBVR (LCSC C7834). U21 channels 2 and 4
  translate SCK and MOSI; the unused channel input/OE pairs are grounded and
  their outputs are NC. This avoids back-powering or driving an unpowered LF
  domain while giving all seven U21 GND pads short inner-plane connections.
- Six global LF/shared-SPI data paths are routed and connectivity-checked:
  SPI_SCK, SPI_MOSI, SPI_MISO, LF_SCLK_5V, LF_DIN_5V and LF_DOUT_5V.
  The LF_RFID_EN U22 enable endpoint is now complete. A redundant +3V3 fanout
  via in the dense PN532/U22 field was removed, R109 now joins the retained
  +3V3 branch directly, and U22.1 reaches the existing LF-enable trunk through
  a 0.45/0.20-mm via and a reviewed 0.20-mm L3 route. L2 remains plane-only;
  `scripts/route_lf_enable.py` reproduces and connectivity-checks this stage.
  The low-current LF_5V branch from the existing distribution via C515 to U21
  is also complete, with a reviewed 0.39-mm TSSOP neckdown.
- The dense U8/U11 sensor corridor is now reworked. U8 SCL reaches U1.6
  through two standard 0.45/0.20-mm through-vias and a short L3 crossing;
  U8 SDA reaches U11.4 directly on B.Cu. `FG_ALERT_N` and `SPI_MOSI` were
  rerouted around the corridor, and the local U11.3/U11.5 ground branches were
  restored. `scripts/route_i2c_sensor_corridor.py` reproduces and checks all
  six affected endpoint pairs while L2 remains ground-only.
- The dense-plane cleanup is complete. All GND and +3V3 islands are now
  closed, including the difficult U2.40, C704 and microSD J2.4 fanouts. U2.40
  uses a reviewed local reroute of LF_SCLK_5V/LF_DIN_5V; C704 is connected on
  B.Cu without a via below U21, and J2.4 reaches the L3 +3V3 plane through a
  clearance-checked 0.45/0.20-mm via.
- The accepted charger and battery-protection power stages are complete.
  VBUS_FUSED now joins F1, D101,
  C106, C103, U5.13 and U16.5 with 0.50/0.60-mm copper and one ordinary
  0.80/0.40-mm via. Both U5 CELL_POS pins join C121/C104 through short local
  neckdowns; the main CELL_POS route then runs at 0.80 mm to J4.1 and reuses
  one capacitor via. CELL_NEG leaves J4.2 without via-in-pad, crosses on F.Cu
  outside the connector pad and reaches Q2 with a reviewed local neckdown.
  BAT_FET_MID joins Q2/Q3 at 0.80 mm, and BAT_COUT plus the U14 CELL_NEG sense
  return complete the local battery-protection block. VSYS now joins U5, U6,
  U7 and the local bulk capacitors through a reviewed B.Cu corridor. R129 is a
  hand-friendly 0805 zero-ohm SPI_MOSI crossover that preserves the solid L2
  GND plane and the protected L3 power corridors.
  Both USB-C VBUS contact groups now reach F1 through 0.30-mm F.Cu pin escapes,
  two 0.70/0.35-mm vias and a short 0.20-mm shield-corridor neck that widens
  immediately to 0.50/0.60 mm. `+5V_RAW` and `+5V_AUX` are now fully connected
  through two narrow, higher-priority L3 corridors; all other 5-V trunks are
  complete as well. L2 remains an uninterrupted GND return plane. L3 retains
  the protected power polygons and may also carry ordinary low-speed digital
  traces outside +5V_RAW/+5V_AUX, RF, USB, NFC and LF analogue keepouts.
- The authoritative PCB now contains 1889 track segments, 343 vias and 23
  zones. The current ratsnest contains 138 open connection items. GND, +3V3,
  +5V_RAW and +5V_AUX are all fully connected.
  Accepted DRC-neutral additions include I2C_SDA, EX0/EX1/EX4 interrupt,
  NFC_I0, SUBGHZ_GDO2, AUX5_EN, I2C_SCL_HDR, SPI_SCK_HDR, GPIO44_MCU, BOOT_N,
  USER_BUTTON_B_N, CHG_DISABLE, USER_BUTTON_SELECT_N, SUB_CS_N, SPI_SCK,
  SD_DETECT_N and AUX5_FAULT_N islands. Both PN532 crystal pins and their
  load-capacitor branches are complete. The two local matching-to-loop paths
  and both external loop feeds are routed through the intended NFC keepout
  corridors; the latter use two ordinary vias and short L3 signal sections.
  The three high-output IR LEDs now share a complete 0.50-mm common-cathode
  route along the short board edge.
- The short PN532 `/NFC_TX2_F` matching branch from L302.2 to C309.1 is now
  complete with five locked 0.15-mm F.Cu segments and no via. Its narrow
  corridor retains 0.25 mm to both C301 and the reviewed LF_DIN_5V crossing;
  `scripts/route_nfc_tx2_matching.py` reproduces and checks this connection.
- The microSD `/SD_CS_DEV` leg now connects U19.1 to J2.2 through one local
  0.45/0.20-mm via. The U19.2 GND escape was shifted toward the card edge while
  retaining the same plane via; `scripts/route_sd_cs_socket.py` reproduces and
  checks both endpoint groups.
- The ESP32 `/SPI_MOSI` pad U1.19 now joins the existing U21/R129 shared-bus
  group through eight locked 0.15-mm F.Cu/B.Cu segments and one 0.45/0.20-mm
  via. `scripts/route_spi_mosi_mcu.py` reproduces and checks the endpoint pair.
- The microSD resistor-side `/SPI_MOSI` pad R511.1 now joins that shared group
  through six locked 0.15-mm L3 segments, one short F.Cu escape and one
  0.45/0.20-mm via. `scripts/route_spi_mosi_sd_resistor.py` reproduces the
  route and checks R511.1 against R129.2 after the plane refill.
- The USB-C `/USB_CC1` receptacle pad J1.A5 now reaches R101.1 through ten
  locked outer-layer segments and one 0.45/0.20-mm via. U16.4 joins J1.A5
  through nine locked 0.20-mm F.Cu segments with no additional via, leaving
  all three CC1 pads in one group. `scripts/route_usb_cc1_resistor.py` and
  `scripts/route_usb_cc1_esd_branch.py` reproduce the two reviewed stages.
- The UP-button `/USER_BUTTON_A_N` contact SW3.1 now reaches R608.1 through
  ten locked 0.15-mm segments, two 0.45/0.20-mm vias and a short reviewed L3
  crossing. U18.11 remains the second net group;
  `scripts/route_user_button_a_pullup.py` reproduces the accepted geometry.
- The back-side `/CELL_POS` monitor input U8.3 now reaches local capacitor
  C120.1 through four locked, via-free 0.15-mm B.Cu segments;
  `scripts/route_cell_pos_monitor.py` reproduces and checks this endpoint pair.
- CELL_NEG test point TP102 now sits directly on the existing front-side
  battery-negative trunk at 103.85,65.75 mm. This closes its isolated group
  without adding copper; `scripts/place_tp102_cell_neg.py` reproduces the
  locked placement and is idempotent.
- CELL_POS test point TP101 now sits on the existing 0.8 mm front-side trunk
  junction at 98.10,62.20 mm. This closes its isolated group without adding
  copper while keeping the probe pad clear of the connector and resistor
  courtyards; `scripts/place_tp101_cell_pos.py` reproduces the locked,
  idempotent placement.
- VBUS_USB test point TP105 now sits on the back at 99.00,50.40 mm, directly
  on the existing 0.5 mm USB input trunk. The opposite side of the USB-C
  receptacle remains free of back-side component courtyards, so the probe pad
  stays accessible; `scripts/place_tp105_vbus_usb.py` reproduces the locked,
  idempotent placement.
- The protected `/GPIO43` output R718.2 now reaches expansion-header pad J5.11
  through three locked 0.15-mm L3 segments and one 0.45/0.20-mm tented via;
  `scripts/route_gpio43_header.py` reproduces and checks this endpoint pair.
- The protected `/SPI_MISO_HDR` branch now joins R734.2 to J5 ESD-array pad
  U25.1 through eleven locked 0.15-mm F.Cu/L3 segments and two tented
  0.45/0.20-mm vias; `scripts/route_spi_miso_header_esd.py` reproduces and
  checks the endpoint pair while leaving the L2 GND plane signal-free.
- The PN532 `/NFC_TX1` output U2.4 now reaches matching inductor L301.1 through
  two F.Cu escapes, three locked 0.20-mm L3 segments and two tented
  0.45/0.20-mm vias. Two local LF clock/data jogs preserve those neighboring
  nets while opening the conventional through-via corridor;
  `scripts/route_nfc_tx1_driver.py` reproduces and checks the endpoint pair.
- The OLED `/OLED_VCC` supply now joins C615/C616 to J8.16 through one
  0.45/0.20-mm tented via, a 0.15-mm L3 crossing and a short F.Cu escape. The
  neighboring `/OLED_C1P` corridor is narrowed locally to the project minimum,
  and one short `+5V_AUX` L3 bridge keeps all five AUX5 pads in one plane group.
  `scripts/route_oled_vcc.py` reproduces and checks all three affected nets.
- Schematic/PCB/design-netlist parity passes, every new route passes explicit
  endpoint connectivity checks, and ERC reports 0 errors / 0 warnings. DRC
  reports 16 known non-release findings: 8 existing clearance findings, one
  J4 copper-to-edge finding, six reviewed footprint-library findings and one
  positionless B.Cu copper-sliver warning created by plane refill. The project
  minima are now 0.15-mm track, 0.45-mm via and 0.20-mm drill; critical power,
  USB, NFC, LF and Sub-GHz nets retain their stricter custom rules. No new
  routing clearance, short, dangling-track or power violation is present.
- This is a reproducible routing checkpoint, not an order-ready board.

## Saved state

- ESP32-S3-WROOM-1-N8R2 remains the V1 controller. ESP32-S31 was reviewed as a
  possible future V2 option but is not being introduced into this revision;
  changing to its larger, preliminary WROOM ecosystem would restart placement,
  routing and firmware validation without improving a current V1 requirement.
- Generated schematic: 274 symbols, 267 footprints and 181 named logical nets;
  the current PCB contains 231 physical nets, including three unique NC nets.
- GNSS hardware has been removed. U4 is now HTRC110 125-kHz LF RFID with
  switched 5 V, level translation, external 4-MHz clock and removable coil.
- ATECC608C-SSHDA-T, decoupling and the dedicated SW6 PAIR button are captured.
- U24/U25 add low-capacitance shunt protection to every exposed J5 I2C/SPI
  line. Direct GPIO and expander pins retain their existing series protection.
- TP601 and TP701-TP703 were removed: the last RGB output is intentionally NC.
  BMI270 retains both interrupt lines; BMP390 pin 7 is intentionally NC and
  pressure data remains available by I2C polling.
- R736/RT701 add a 10-kohm B3950 board-temperature divider on GPIO9 in the
  former 4 x 4 mm back-side pocket. J5 pin 7 is now probe-only through R714;
  eleven direct expansion GPIOs remain.
- Stale GNSS DNP rules were removed from the manufacturing exporter; its safety
  defaults now match the NFC, Sub-GHz and LF tuning footprints in this design.
- Repository cleanup removed the superseded placement render, empty capture-target
  sheets, generated PCB/autorouter intermediates and their duplicate DRC reports.
  These staging files are reproducible from `hardware/scripts` and are now ignored;
  the main schematic and `PocketLab-Card.kicad_pcb` remain authoritative.
- The placement builder fits all 267 footprints (131 front, 136 back) without
  unapproved courtyard, keepout or board-inset collision.
- The authoritative routed checkpoint contains 130 front and 137 back
  footprints. The one-part-per-side difference from the regenerated placement
  donor is limited to a generic unrouted packing slot; all 267 references exist.
- U4 and its LF resonance, current-limit, RX, oscillator and coil-interface
  parts now form one compact back-side analog island beside J3. C506/C507/C508
  are aligned as a straight tuning bank; C507/C508 remain DNP options.
- `LF_TX1`, `LF_TX2`, `LF_ANT_A`, `LF_ANT_B`, `LF_TAP`, `LF_RX`, `LF_CEXT`,
  `LF_QGND` and `LF_CLK_4M` are completely routed on B.Cu with no vias.
  The front-side U17 support island and its LF 5-V distribution to U4, Y501
  and C513 are also routed. A narrowly scoped U17 pin-escape rule permits a
  0.20-mm SOT-23 neckdown; three ordinary tented through-vias carry LF 5 V.
- The placement generator reserves the LF support route corridor so future
  regenerations cannot silently pack unrelated parts into it. The LF analog,
  placement-merge and support routes are reproducible with the three dedicated
  scripts in `hardware/scripts`.
- The solid L2 GND plane and mixed-power L3 plane regenerate successfully;
  the L3 +5V polygons are protected from ordinary-signal routing.
- The PCB stack is now the nominal 1.2-mm, four-layer
  `JLC04121H-7628` target with 1-oz outer / 0.5-oz inner copper and ENIG.
  Confirm the live factory stack before freezing USB and RF widths.
- Routing has started. The main PCB contains the guarded digital fanout and
  DRC-clean local routes for U6_L1, U6_L2, U7_SW, RTC_OSCI and RTC_OSCO. U6
  input/output power, U7 input/output power, U7 feedback and the low-current
  5-V feedback sense branch are now also routed. The nearby IR branch was
  rerouted around Y701, and R405 now faces U3 -> antenna. CC2 and the provisional
  Sub-GHz feed are also complete. The subsequent split-5V and dense-plane
  stages formed the earlier 1214-segment checkpoint; the current authoritative
  board has advanced to 1889 segments, 343 vias and 23 zones.
- Two named B.Cu rule areas limit U7's unavoidable 0.20-mm power-pin neckdowns
  to the package exits and the reviewed Kelvin/sense corridor; the power rails
  widen to 0.50 mm outside those areas.
- The accepted stage-5 pass completes VSYS without inner-layer signal copper.
  R129 (C17477) splits SPI_MOSI through a real 0805 series crossover, and the
  corridor uses a documented 0.30-mm VSYS neck only where required.
- The accepted stage-6 pass completes the local USB-C VBUS-to-F1 connection.
  Its two connector escapes and shield-corridor neck are bounded by three named
  rule areas; all longer input-current sections use 0.50/0.60-mm copper.
- R201/R202 are now vertical, side-by-side 0805 parts. The native USB pair is
  fully routed from U1 through both 22-ohm resistors and U16 to all four J1
  A6/B6/A7/B7 data pads. The MCU side is via-free; the connector bridge uses
  five 0.50/0.30-mm vias and a local 0.15-mm fine-pitch clearance area.
- U16 moved 0.55 mm inward and R102 is now a fixed front-side 0805 below the
  USB data crossover. J1 B5, R102, its local ground return and the U16 pin-6
  protection branch are routed. CC2 changes layer with two standard
  0.50/0.30-mm through vias and has no signal trace on L2/L3. The narrowly
  scoped 0.20-mm C103/C104 escape rule leaves every other power-pair clearance
  at 0.25 mm. R101 remains a fixed 0805 on the back beside the right edge.
- The Sub-GHz pi network is now routed at the provisional 0.36-mm width. C404
  is perpendicular to the feed, `/SUBGHZ_RF_MOD` stays on B.Cu and
  `/SUBGHZ_RF_ANT` makes exactly one 0.60/0.30-mm transition to F.Cu before the
  spring pocket. The front trace stays outside the antenna notch. The obsolete
  `/OLED_VCC` autorouter path through C404's corrected ground-shunt position was
  removed and has since been replaced by the reviewed local OLED route above.
- SW3/SW7/SW4 now form a compact, labeled UP/OK/DOWN row using the same
  KMR221GLFS footprint as RESET/BOOT. R607 is the SELECT pull-up; U9/P07 is the
  SELECT input and BMP390 INT is NC. PAIR remains a separate security button.
- SPI_SCK, SPI_MOSI, SPI_MISO, I2C_SCL, GPIO44_MCU, NFC_DVDD, the three user
  button nets, IR_LED_A1, IR_LED_K, GPIO43, NFC_LOADMOD, NFC_RESET_N,
  I2C_SDA, PAIR_N, SPI_MOSI_HDR, SPI_SCK_HDR, LF_DOUT_5V and OLED_VCC
  autorouter copper was removed as complete nets where it crossed the accepted
  USB/button/LF placement. This describes the earlier cleanup; several of those
  islands have since been rerouted with the guarded deterministic router. The
  remaining items stay in the ratsnest without dangling copper stubs.
- The final saved KiCad checks pass schematic/PCB parity and ERC with 0 errors /
  0 warnings. DRC reports 16 known non-release findings: 8 clearances, one J4
  copper-to-edge finding, six local footprint-library comparison warnings and
  one positionless B.Cu copper-sliver warning. There are 138 open connection
  items, so this remains only a routing checkpoint.
- Firmware remains at the preceding two-button checkpoint by explicit project
  priority; no firmware file was changed for the new SELECT hardware. That
  previous ESP32-S3 build provides LF power sequencing,
  HTRC110 configuration/phase/antenna diagnostics, ATECC presence detection,
  a physical 60-second pairing gate, web control, microSD, bounded NEC IR and
  board-temperature readout/80-degree-C shutdown with 70-degree-C hysteresis.
- Secure-element zone locking, persistent owner keys and the phone app are not
  implemented. No irreversible eFuses or ATECC locks have been applied.

## Next engineering steps

1. Reroute the remaining digital/control nets, including the short U24/U25
   clamp branches, then hand-route the dense ESP_EN, BMI/IO-expander and
   microSD signal corridors that the guarded router correctly skips. Refill
   planes after each accepted batch and close all 138 ratsnest items.
2. Complete the PN532 DVDD and TX matching network with reviewed short RF
   paths; preserve the antenna keepout and tune the populated V1 board.
3. Confirm the live `JLC04121H-7628` data and recalculate the stack-dependent
   USB and provisional 0.36-mm Sub-GHz geometry. Retune the assembled pi
   network before treating the RF path as production-final.
4. Run full KiCad DRC, schematic/PCB parity, independent footprint review and
   JLCPCB DFM/BOM/CPL preview.
5. Assemble a small bring-up batch; current-limit first power-up and measure
   every rail before fitting the external coil.
6. Measure the real coil, choose C506/C507/C508, check HTRC phase/ANTFAIL,
   current, voltage and read range.
7. Keep firmware/app work deferred until the PCB routing and manufacturing
   package are complete; then update the input map for UP/OK/DOWN before any
   ATECC provisioning or locking work.

This checkpoint is not an order-ready fabrication release.

## Paused PCBA/layout work (2026-08-17)

Work was paused at the user's request after the first direct-assembly and
compact-layout pass.  The authoritative `hardware/PocketLab-Card.kicad_pcb`
remains the last promoted board and is intentionally not replaced by an
unfinished candidate.

- All 237 populated BOM rows now have an LCSC identifier in the generated
  design data.  Safe commodity passives are assigned smaller 0402/0603
  packages; power, pulse, RF and reviewed crossover parts remain larger where
  their electrical or layout margin matters.
- The through-hole, hand-bent TSAL6200 emitters and the larger user switches
  have direct-assembly SMD replacement footprints.  Their source footprints,
  generator policy and migration/routing helpers are preserved in this branch.
- The best preserved routing candidate is
  `hardware/checkpoints/PocketLab-Card-pcba-routing-wip.kicad_pcb`.  It includes
  the accepted NFC_I0 and SPI_MISO repairs and one +3V3 stitching via.
- Its last full project-context DRC recorded 46 rule findings and 158 open
  connection items.  The higher finding count than the authoritative board is
  primarily the honest result of checking the migrated fine-pitch/custom
  footprints in full project context; it is not a release result.
- A subsequent compact-cleanup script and narrowed custom-clearance rules are
  saved but deliberately not executed/accepted after the pause request.

Resume from the checkpoint candidate, run the compact repair on a new copy,
compare DRC and connectivity, and only then promote a verified candidate to the
authoritative PCB.
