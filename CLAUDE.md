# Workspace conventions

## Languages — fixed per device

| Project | Language |
|---|---|
| Hub_Server | **Python 3** (CPython on Pi 5) |
| Hub_Server_Firmware | **MicroPython** (RP2040, Pico A) |
| PicoB | **MicroPython** (RP2040) |
| ESP32_C3 | **Arduino C++** (PlatformIO) |
| Trip_Hub | **Python 3** (Flask, on Pi 5) |

Do not suggest porting between languages. Each device's toolchain is
locked. PicoB is **proven, in-production code** — treat it as the
spec, not a refactor target.

## Shared protocol facts

- **Radio:** SX1278 / SX1276 family, 434 MHz, SF9/BW125/CR4-5,
  sync word `0x34`, CRC on, preamble 8.
- **Encryption:** AES-128-ECB, key `LoRaMeshDemoKey1` (16 bytes, demo —
  change for your own deployment). Same key on every node.
- **Hub_Server ↔ Hub_Server_Firmware USB serial grammar** lives in
  [Hub_Server_Firmware/CLAUDE.md](Hub_Server_Firmware/CLAUDE.md) (with
  the host-side view in [Hub_Server/CLAUDE.md](Hub_Server/CLAUDE.md)).
- **LoRa message types** (CHAT / GPS / TRIPSTART / TRIPEND / SYNC /
  RTRIPS / RTRIP / RPTS / QTRIPS / QTRIP / QPTS / ACK) are documented
  per-project in the relevant `CLAUDE.md`. Every payload starts with
  one of these uppercase prefixes followed by `:`. Untagged packets
  are treated as unknown — logged, not routed to chat.
- **Chat plaintext cap**: 220 B body (so `CHAT:<body>` ≈ 225 B fits in
  one AES-padded 250 B LoRa packet). Multi-packet fragmentation is not
  implemented; senders reject overflow with a clear error.

## Workspace docs

- [README.md](README.md) — top-level overview, who-does-what.
- [docs/development.md](docs/development.md) — developer/ops runbook: connect,
  flash, inspect the DB, deploy to the bridge, capture screenshots. Uses
  placeholder hosts/creds — never commit real ones.
- [docs/](docs/) — architecture, protocols, hardware, per-device guides.
- [scripts/](scripts/) — dev tooling (screenshots, DB inspect, serial, diagnostics).
- [esp32_projects.code-workspace](esp32_projects.code-workspace) —
  VSCode multi-root config.
