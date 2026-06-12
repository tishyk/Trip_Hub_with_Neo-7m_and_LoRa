# Dev scripts

Cross-cutting developer tooling. Everything takes hosts / paths / ports via
**arguments or environment variables** — no credentials are baked in. See the
[developer guide](../docs/development.md) for the full workflow.

| Script | What it does | Example |
|---|---|---|
| [`capture_screenshots.py`](capture_screenshots.py) | Headless Playwright capture of the Trip Hub dashboard → `docs/assets/` | `BASE_URL=http://raspberrypi.local:5000 python scripts/capture_screenshots.py` |
| [`db_inspect.py`](db_inspect.py) | Quick SQLite read-out: latest trips, device presence, a trip’s point span | `python scripts/db_inspect.py --db /path/trips.db` |
| [`compare_trip_jumps.py`](compare_trip_jumps.py) | Fix-to-fix jump distribution per trip (GPS quality / outlier diagnostic) | `python scripts/compare_trip_jumps.py --db /path/trips.db 168 167` |
| [`read_serial.py`](read_serial.py) | Read a device’s serial output; `--no-reset` avoids rebooting the ESP32-C3 | `python scripts/read_serial.py COM9 --no-reset --seconds 6` |

## Prerequisites

```bash
pip install playwright pyserial && playwright install chromium
```
(`sqlite3` ships with Python.)

## Whole-Pi deploy

- [`../sync_pi5.sh`](../sync_pi5.sh) — one command to push `Hub_Server` /
  `Hub_Server_Firmware` / `Trip_Hub` to the Pi, flash Pico A via `mpremote`, and
  (re)start both tmux services. Configure via `$PI_HOST` / `$PI_DEPLOY_DIR`.

## Related per-project utilities & self-tests

- `PicoB/usb_dump_trips.py` — dump a tracker’s stored trips over USB (MicroPython).
- `PicoB/import_backup_trips.py` — POST archived `T*.gps/.json` trips to Trip_Hub.
- `PicoB/cleanup_short_trips.py` — prune short/noise trips on device + hub.
- `Hub_Server/recover_trip_from_log.py` — reconstruct a trip from `picoA_serial.log`.
- `python PicoB/sync_codec.py` · `sync_manager.py` · `trip_storage.py` ·
  `Hub_Server/splitter.py` — run any directly to execute its built-in self-tests.

> All host/path defaults in these scripts are placeholders
> (`raspberrypi.local`, `/home/pi/…`) — pass your own via flags/env.
