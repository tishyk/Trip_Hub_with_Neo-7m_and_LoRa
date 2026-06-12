# Developer guide

How to connect to, flash, inspect and deploy every device in the fleet — plus
the data you’ll want while developing. This doubles as the operating manual for
automated assistants (e.g. Claude Code) working in this repo.

> **Credentials & hosts are placeholders.** Set these for your own setup; never
> commit real values (see [../SECURITY.md](../SECURITY.md)).
>
> ```bash
> export PI_HOST=raspberrypi.local        # your Pi's hostname or IP
> export PI_USER=pi                        # your Pi login
> export PI_DIR=/home/$PI_USER/esp32_projects   # deploy dir on the Pi
> export DB=$PI_DIR/Trip_Hub/trips.db      # SQLite path on the Pi
> ```
> A convenient `~/.ssh/config` alias keeps commands short:
> ```
> Host pi   HostName raspberrypi.local   User pi
> ```
> Serial ports differ by OS: **Windows** `COM3`, `COM9`, … · **Linux/macOS**
> `/dev/ttyACM0`, `/dev/tty.usbmodem*`.

## Toolchains

| Tool | For | Install |
|---|---|---|
| **PlatformIO** | ESP32-C3 build/flash | `pip install platformio` (or VS Code extension) |
| **mpremote** | MicroPython Picos | `pip install mpremote` |
| **Python 3** | Pi services, scripts | system / `python.org` |
| **pyserial** | reading device serial | `pip install pyserial` |
| **Playwright** | dashboard screenshots | `pip install playwright && playwright install chromium` |
| **sqlite3** | DB inspection | bundled with Python (`import sqlite3`) or the CLI |

---

## ESP32-C3 (Arduino C++)

**Connect:** USB-C; it enumerates as a USB serial device. **Note:** the C3 uses
native USB — toggling DTR/RTS resets it, so a naive serial open reboots the board
(use [`scripts/read_serial.py --no-reset`](../scripts/read_serial.py)).

```bash
cd ESP32_C3
pio run                                  # compile
pio run -t upload                        # flash (preserves LittleFS!)
pio run -t upload --upload-port COM9     # pin the port if needed
pio device monitor -b 115200             # serial console
```
A plain `upload` rewrites only the app partition — **trips/chat on LittleFS
survive**. Avoid “Erase Flash” / “Upload Filesystem Image” unless you mean it.

**Read its data over WiFi** (join AP `LoraWan`, then):
```bash
curl http://192.168.4.1/api/devices       # liveness roster + signal
curl http://192.168.4.1/api/trips          # trips on flash
curl "http://192.168.4.1/api/trip?id=T1749330565"   # one trip + fixes
```

---

## MicroPython Picos (Pico A bridge, Pico B tracker)

```bash
PORT=/dev/ttyACM0                          # or COMx on Windows
mpremote connect $PORT fs ls               # list flash
mpremote connect $PORT fs cp *.py :        # deploy all modules
mpremote connect $PORT fs cp file.py :file.py
mpremote connect $PORT fs cat sync_state.json
mpremote connect $PORT reset               # restart into new code
mpremote connect $PORT repl                # interactive REPL (Ctrl-] to exit)
```

**Dump a tracker’s trips over USB** (Pico B):
```bash
mpremote connect $PORT run PicoB/usb_dump_trips.py
```

> Only one process can own a Pico’s serial port. If the Pi’s bridge driver
> (`hub.py`) is running, stop it before `mpremote` (see *Deploying to Pico A*).

---

## Raspberry Pi 5 gateway

```bash
ssh $PI_USER@$PI_HOST          # or: ssh pi   (with the alias above)
```

Two long-lived services (run here under `tmux`):

```bash
# router / LoRa bridge driver (owns the Pico A serial port)
tmux new-session -d -s hub_server -c $PI_DIR/Hub_Server \
    "python3 hub.py --server http://localhost:5000"

# web dashboard
tmux new-session -d -s trip_hub   -c $PI_DIR/Trip_Hub \
    "python3 trip_server.py"           # http://$PI_HOST:5000

tmux ls                                  # what's running
tmux capture-pane -t hub_server -p | tail -40   # recent router output
```
Restart either independently; **both must be up** (it’s easy to restart the
router and forget the web app on `:5000`). Logs live in `Hub_Server/logs/`.

---

## Inspecting the data (SQLite)

```bash
# latest trips with point counts and sync status
ssh pi "sqlite3 $DB 'SELECT id, device_id, start_time, end_time, distance_km,
        movement_type, (SELECT COUNT(*) FROM trip_points tp WHERE tp.trip_id=t.id)
        AS npts, sync_status FROM trips t ORDER BY id DESC LIMIT 12;'"

# device roster / presence (drives the chat dots)
ssh pi "sqlite3 $DB 'SELECT name, id, last_seen, last_rssi FROM devices ORDER BY name;'"

# point span for one trip
ssh pi "sqlite3 $DB 'SELECT MIN(timestamp), MAX(timestamp), COUNT(*)
        FROM trip_points WHERE trip_id=168;'"
```
Or, locally with the helper: [`scripts/db_inspect.py`](../scripts/db_inspect.py).

The dashboard’s **Activity Log** tab (`http://$PI_HOST:5000`) is the live view of
decrypted radio traffic (`DEVICE:` heartbeats, `QPTS`/`RPTS`/`ACK`, `SYNC`).

---

## Deploying to Pico A (the bridge)

Pico A is attached to the Pi and its serial port is held by `hub.py`, so:

```bash
ssh pi '
  MPR=~/.local/bin/mpremote
  tmux kill-session -t hub_server                 # free /dev/ttyACM0
  sleep 2
  cd '"$PI_DIR"'/Hub_Server_Firmware
  $MPR connect /dev/ttyACM0 fs cp lora_bridge.py :lora_bridge.py
  $MPR connect /dev/ttyACM0 reset
  tmux new-session -d -s hub_server -c '"$PI_DIR"'/Hub_Server \
      "python3 hub.py --server http://localhost:5000"
'
```

---

## Screenshots of the dashboard

```bash
BASE_URL=http://$PI_HOST:5000 python scripts/capture_screenshots.py
# writes docs/assets/trip-hub-*.png
```
The ESP32 UI lives on its own softAP, so capture those from a device joined to
`LoraWan` (see [web-interfaces.md](web-interfaces.md)).

---

## Built-in self-tests (no hardware)

Several modules self-test when run directly:

```bash
python PicoB/sync_codec.py        # RPTS delta codec round-trips
python PicoB/sync_manager.py      # sync state machine against a fake DB
python PicoB/trip_storage.py      # trip storage + resume logic
python Hub_Server/splitter.py     # payload routing / GPS+trip parsing
```

---

## Dev scripts

Cross-cutting tooling lives in [`scripts/`](../scripts/) — see
[scripts/README.md](../scripts/README.md). All take hosts/paths via args or env
so there are no baked-in credentials.
