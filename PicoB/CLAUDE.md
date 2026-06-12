# PicoB — battery-powered LoRa GPS tracker (reference implementation)

## Role
Standalone Pi Pico (RP2040) with SX1278 LoRa + NEO-7M GPS, runs from a power bank, no host attached. Generates GPS broadcasts, detects trips, persists them on flash, syncs the backlog to the Pi 5 (via Pico A bridge) when in range.

## Language — fixed
**MicroPython.** Do not port these files to C++. The ESP32-C3 in [../ESP32_C3/](../ESP32_C3/) is being **reimplemented** in Arduino C++ using PicoB as the *behavioral spec* — not as source for cross-compile.

## What to use this folder for
- **Specification**: cadences, hysteresis bands, trip-detection thresholds, file formats, sync state machine. When a question comes up about "what should the ESP32 do here?", read these files.
- **Reference data**: real recorded trips in [trips/](trips/) (`.gps` = one fix per line `[ts,lat,lon,alt,speed]`, `.json` = trip metadata). Use to validate the ESP32 port produces compatible files.

## Boot
[main.py](main.py) wraps `print` in try/except (USB buffer fills with no host attached → would hang otherwise), blinks onboard LED twice, then runs [runtime.py](runtime.py) `chat()` in standalone mode. Crashes get logged to `boot_errors.log` and trigger soft-reboot. (`chat()` is the historical function name — it now runs the whole node, not just chat.)

## Where to tune values
- [config.py](config.py) — single inventory of every field-tunable
  constant on PicoB: device id, LoRa air params + AES key, pin maps,
  cadence-by-class, hysteresis bands, trip thresholds, flash buffering,
  sync retries. Modules are migrating to `from config import …` with a
  fallback to local defaults; today only [trip_storage.py](trip_storage.py)
  reads from there. Anywhere else, treat config.py as the canonical
  reference and keep both copies in sync until the migration completes.

## Key modules
- [runtime.py](runtime.py) — device main loop (radio + AES + GPS + tracker + sync + chat + QPOS/WHO). `DEVICE_ID` is loaded from `device_id.txt` on flash (renameable over LoRa via the DEVICE: rename protocol; current name on this unit is `Picos-B1`); `DEVICE_HWID` comes from `machine.unique_id()`. Cadence by class: idle 60s, walking 15s, cycling 10s, auto 10s. Hysteresis: walk→cycle at 8 km/h up, cycle→walk at 6 km/h down; cycle→auto at 27 km/h up, auto→cycle at 23 km/h down.
- [trip_tracker.py](trip_tracker.py) — IDLE↔MOVING state machine. Trip ends via stop detection (not speed-class crossing): walking 120s stationary, cycling 60s, auto 300s. Final classification = peak sustained speed over any 5-min window across the trip.
- [trip_storage.py](trip_storage.py) — writes per-trip `.gps` + `.json` to flash.
- [sync_codec.py](sync_codec.py) + [sync_manager.py](sync_manager.py) — encode/decode the SYNC/RTRIPS/RTRIP/RPTS messages. Mirror of Pi 5 side in [../Hub_Server/sync_manager.py](../Hub_Server/sync_manager.py).
- [gps_module.py](gps_module.py), [sx127x.py](sx127x.py) — NMEA + radio drivers.

## State persisted across reboots
- [in_progress.txt](in_progress.txt) — active trip ID (single line) so a power-cycle mid-trip can resume.
- [sync_state.json](sync_state.json) — per-trip sync status (UNSENT / SENT / CONFIRMED).
- [trips/T*.gps](trips/) + [trips/T*.json](trips/) — the trip log.

## Known traps
- Old pinout (matches the original Pico-Pico debug session): SCK=2, MOSI=3, MISO=4, NSS=5, RST=22, DIO0=26. Different from Pico A.
- [runtime.py](runtime.py) was renamed from `lora_chat.py` and the in-file docstring used to say "SF7 to match bridge" — the real config runs SF9 across the network. Trust the live config in code, not the doc.
- `cryptolib` AES mode availability differs between MicroPython builds — code probes CBC then falls back to ECB. Network is ECB.

## Don't add features here
This is the proven battery tracker. New work goes into the ESP32-C3 project. PicoB stays as it is so we always have a known-good comparison node.
