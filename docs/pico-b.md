# Pico B — battery GPS tracker (reference node)

A standalone, battery-powered LoRa GPS tracker. It runs from a power bank with no
host attached, generates GPS broadcasts, detects and stores trips, and syncs them
to the hub when in range. It is the **behavioural reference** the ESP32-C3 port
mirrors — proven, in-production code.

| | |
|---|---|
| **Folder** | [../PicoB/](../PicoB/) |
| **MCU** | Raspberry Pi Pico (RP2040) |
| **Runtime** | MicroPython |
| **Radio / GPS** | SX1278 · NEO-7M |
| **Pinout** | [hardware.md → Pico B](hardware.md#pico-b--battery-tracker-picobconfigpy) |

## What it does

- **GPS cadence by movement class** with hysteresis: idle 60 s, walking 15 s,
  cycling 10 s, auto 10 s; walk→cycle at 8 km/h up / 6 down, cycle→auto at 27 up /
  23 down.
- **Trip detection** — IDLE↔MOVING with stop-detection timeouts per class
  (walking 120 s, cycling 60 s, auto 300 s stationary). Final classification is
  the peak sustained speed over any 5-minute window.
- **On-flash trip log** — per-trip `.gps` + `.json`, buffered writes
  (`FLUSH_EVERY`), `in_progress.txt` for power-cycle resume, `sync_state.json`
  for per-trip UNSENT/SENT/CONFIRMED.
- **Store-and-forward sync** — `SYNC:` on boot/after-trip/retry, answers the Pi’s
  `Q*` queries, marks CONFIRMED on `ACK`.
- **Presence** — `DEVICE:` heartbeat every 60 s; keeps an in-RAM roster of peers.
- **Resilience** — stale `in_progress` trips are auto-closed on boot; a watchdog
  force-closes a trip if GPS is lost mid-journey.

## Source layout ([../PicoB/](../PicoB/))

| File | Role |
|---|---|
| `runtime.py` | device main loop (radio + AES + GPS + tracker + sync + chat + presence) |
| `trip_tracker.py` | IDLE↔MOVING state machine |
| `trip_storage.py` | per-trip `.gps`/`.json` flash storage + sync state |
| `sync_codec.py` / `sync_manager.py` | RPTS codec + sync logic (mirror of the Pi side) |
| `gps_module.py` / `sx127x.py` | NMEA + radio drivers |
| `config.py` | device id, air params, AES key, pin map, cadences, thresholds |

## Deploy (MicroPython)

```bash
mpremote connect <PORT> fs cp *.py :
mpremote connect <PORT> reset
```
`DEVICE_ID` is read from `device_id.txt` on flash (renameable over LoRa via the
`DEVICE:` protocol); `DEVICE_HWID` comes from `machine.unique_id()`.

## Why it’s the reference

This node is kept stable on purpose: it’s the known-good implementation of the
trip-detection thresholds, sync state machine and file formats. When porting to
new hardware (as the ESP32-C3 does), Pico B’s behaviour is the spec — not a
refactor target.
