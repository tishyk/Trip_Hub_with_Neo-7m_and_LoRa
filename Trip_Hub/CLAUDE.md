# Trip_Hub — Pi 5 trip viewer & sync orchestrator

Flask web app that runs on the **Raspberry Pi 5**. Imports GPS trips
from PicoB and ESP32_C3 via the LoRa sync handshake, persists them in
SQLite, serves a map viewer, and tracks per-profile statistics.

## Language — fixed
**Python 3** ([trip_server.py](trip_server.py),
[import_trips.py](import_trips.py)). Frontend is a single HTML file
([index.html](index.html)) with no build step.

## Where data actually lives

On the Pi, the live store is `~/trip_data/`:
- `trips.db` — SQLite, all trips and per-fix samples
- `profiles.json` — movement profiles (walking/cycling/auto)
- `deleted_trips.json` — soft-delete tombstones for sync skip-list
- `receiver.log` — written by trip_server.py at runtime

The copies committed at the root of this folder
([trips.db](trips.db), [profiles.json](profiles.json),
[deleted_trips.json](deleted_trips.json)) are dev fixtures captured
from a real Pi run, useful for local map_viewer development. Do not
treat them as authoritative — the Pi is the source of truth.

## Trip ingestion path

1. **Live broadcasts** — Pico A's bridge forwards `GPS:` payloads to
   the Pi 5 via USB serial. `Hub_Server/splitter.py` POSTs each fix
   to `trip_server.py`'s `/api/live_point`.
2. **Trip files** — at trip end, the field tracker (PicoB / ESP32_C3)
   bundles the trip and either auto-syncs over LoRa
   ([../Hub_Server/sync_manager.py](../Hub_Server/sync_manager.py))
   or has its `T*.gps` + `T*.json` dropped into [incoming/trips/](incoming/trips/)
   for [import_trips.py](import_trips.py) to bulk-import.

## Sync protocol entry points

LoRa sync messages handled by `Hub_Server/sync_manager.py`:
- `SYNC` / `RTRIPS` / `RTRIP` / `RPTS` — discovery + retrieval phases
- `QTRIPS:` / `QTRIP:` / `QPTS:` / `ACK:` — Pi-originated queries

Trip_Hub doesn't talk LoRa directly; everything goes through the
Pi 5 serial bridge to Pico A. See
[../Hub_Server_Firmware/CLAUDE.md](../Hub_Server_Firmware/CLAUDE.md) for the wire grammar.

## Run locally

```bash
cd Trip_Hub
python3 trip_server.py     # serves http://localhost:5000
```

`Path.home() / 'trip_data'` is created if missing. To use the dev
fixtures, symlink or copy `trips.db` etc into `~/trip_data/` first.
