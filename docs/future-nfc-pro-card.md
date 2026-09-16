# Future concept: PocketLab NFC Pro

Status: parked for later; not part of the current BomberCard V1 routing.

## Decision

Build the maximum-performance NFC system as a separate card. Do not add it to
the current multifunction BomberCard. A dedicated board gives the antenna,
matching network, sensitive analogue paths and real-time processing enough
space and avoids interference from Wi-Fi, displays, IR and the other power
domains.

The preferred first board is NFC-only at 13.56 MHz. If advanced 125/134.2-kHz
LF RFID is wanted later, implement it as a second sister card with its own coil
and analogue frontend. Combining both antennas on one credit-card PCB is
theoretically possible, but would compromise the performance and tuning of both
systems.

## Proposed NFC Pro architecture

```text
USB-C / optional battery
          |
    power management
          |
     main real-time MCU <-> FPGA <-> NFC frontend <-> tunable matching <-> large HF loop
          |                 |              |
   flash / microSD      raw capture     RF measurement path
          |
    secure element
```

Preferred laboratory-oriented implementation:

- ST25R3916B high-performance 13.56-MHz frontend for NFC-A/B/F/V, automatic
  antenna tuning and transparent/stream modes.
- High-performance real-time MCU, with the i.MX RT1170 family as the initial
  candidate.
- FPGA for cycle-accurate modulation, demodulation, timestamping, raw-frame
  capture and experimental authorised emulation.
- Secure element for owned test credentials, device identity and signed
  firmware.
- Large perimeter PCB antenna with a copper-free keepout on every layer.
- Switchable C0G matching bank, DNP tuning positions, VNA pads and field/current
  measurement points.
- U.FL or SMA option for interchangeable external antennas and a separate
  receive/sniffer connection.
- USB high-speed streaming, microSD and QSPI flash for long RF captures.
- Likely six-layer central digital island; antenna area kept free of planes and
  unrelated routing.

Alternative product-oriented implementation:

- PN7642 when maximum standards integration, strong reader operation and
  conventional card emulation are more important than raw waveform access.
- It integrates a Cortex-M33, hardware security, broad NFC/MIFARE protocol
  support and a high-power transmitter.

## Possible LF RFID sister card

- Separate large 125/134.2-kHz coil.
- Half-bridge or full-bridge transmitter with controlled current.
- Low-noise, variable-gain receive chain plus comparator and raw ADC path.
- FPGA support for ASK, FSK and PSK capture/generation.
- Switchable resonance capacitor bank and connector for external coils.
- Shared host software and USB protocol with NFC Pro, but no shared antenna.

## Physical direction

- NFC-only can realistically retain the 85.60 x 53.98-mm card outline.
- The assembled device will be thicker than a payment card because of USB,
  connectors, power components and test interfaces.
- A combined maximum-performance NFC+LF instrument should instead use two
  cards or a larger enclosure; fitting both onto one card is not the preferred
  architecture.

## Scope boundary

The intended scope is authorised RF/protocol analysis, antenna development,
reading supported tags and emulating credentials owned for testing. The
hardware does not recover non-exportable keys from secure MIFARE DESFire/Plus
credentials and does not make exact cloning of protected credentials possible.

## Resume point

When this project is resumed:

1. Freeze the use cases: reader range, sniffing, card emulation and required
   NFC standards.
2. Choose between ST25R3916B plus FPGA (laboratory flexibility) and PN7642
   (integrated protocol/product path).
3. Fix the antenna outline and target inductance before placing electronics.
4. Create the schematic, RF matching worksheet and power budget as a new
   project rather than modifying BomberCard V1.
