# BomberCard firmware

Buildable ESP32-S3 bring-up firmware for hardware revision 1. It targets the
ESP32-S3-WROOM-1-N8R2 with Arduino-ESP32 3.x and PlatformIO.

## Implemented baseline

- WPA2 Wi-Fi SoftAP, REST API, read-only WebSocket status and mobile web UI
- microSD mount, listing, download, upload, deletion and remount
- safe boot states for CC1101, microSD, IR, RGB, LF RFID and exposed GPIOs
- read-only CC1101 identity, PN532 presence and both I/O-expander probes
- HTRC110 5 V power sequencing, three-wire command transport, configuration
  readback, phase/sampling setup and antenna-failure diagnosis
- ATECC608C wake/presence probe without changing or locking its configuration
- physical PAIR-button gate for a 60-second pairing window
- bounded NEC IR transmission through the three emitters
- 16-sample GPIO9 board-NTC measurement, web status and 80-degree-C thermal
  shutdown of charging and 5-V loads with 70-degree-C release hysteresis

Tag protocol framing/decoding, secure-element provisioning, persistent owner
keys, the phone app, PN532 transactions, CC1101 receive/decoding, IR decoding,
sensor drivers and battery telemetry remain separate follow-up work. In
particular, an open pairing window is not yet an owner lock.

## Build

```powershell
pio run
pio run -t upload
pio device monitor
```

The 2026-08-11 validation used Arduino-ESP32 3.3.8 and WebSockets 2.7.3. The
release build used 49,568 bytes RAM and 1,113,399 bytes application flash.

At boot, USB CDC prints the generated prototype access point credentials and
`http://192.168.4.1/`. The per-device password and boot-session HTTP token are
for local bring-up; they are not a substitute for the later ATECC-backed app
authentication protocol.

## Safety policy

`platformio.ini` enables only bounded IR transmission. Sub-GHz transmission
and arbitrary GPIO outputs stay compiled out. LF RFID powers up only on an
authenticated local request: the 5 V boost is enabled first, then the HTRC110
rail; shutdown happens in the reverse order. HTRC tag writing is not exposed.
The internal NTC blocks new LF/boost/IR requests at 80 degrees C, disables
charging and existing 5-V loads, and releases only the charger below 70 degrees
C. Loads remain off until the app explicitly requests them again.
An open or shorted divider is treated fail-safe like an overtemperature event.

## HTTP interface

Read-only endpoints include `/healthz`, `/api/config`, `/api/status`,
`/api/hardware`, `/api/gpio`, `/api/files` and `/api/file`.

Mutations require the current `X-BomberCard-Token` header:

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/api/lf/power?enabled=1` | Sequence LF 5 V and run the HTRC self-test |
| `POST` | `/api/lf/diagnose` | Repeat configuration, phase and antenna checks |
| `POST` | `/api/security/pairing-window` | Open 60 s only while PAIR is held and ATECC is present |
| `POST` | `/api/ir/tx?address=0&command=0&repeats=0` | Send one bounded NEC frame |
| `POST` | `/api/sd/remount` | Remount microSD |
| `POST` | `/api/upload?path=/uploads/name` | Upload a file |
| `DELETE` | `/api/file?path=/uploads/name` | Delete a file or empty directory |

## First-board checks

- U9/TCA9535 at `0x20`, U18/TCA9534 at `0x21`, PN532 at `0x24`
- ATECC608C-SSHDA default address `0x60`; keep it unprovisioned until the app
  key format, recovery process and production lock manifest are reviewed
- U18 P4 is `LF_RFID_EN`; boot latch keeps LF 5 V and boost off
- LF pins: GPIO18 SCLK, GPIO21 DOUT, GPIO35 DIN; GPIO38 is active-low PAIR
- GPIO9 is the internal board-temperature ADC and J5 pin 7 is probe-only
- HTRC110 page readback, `ANTFAIL`, measured phase and tuned external coil
- CC1101 identity and shared-SPI chip-select idle levels

`include/board_pins.h` mirrors `docs/pinout.md`.
