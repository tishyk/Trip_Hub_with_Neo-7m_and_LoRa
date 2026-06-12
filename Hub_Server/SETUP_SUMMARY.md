# Hub_Server — Pi 5 setup

First-time setup for running [hub.py](hub.py) on a
Raspberry Pi 5 (Debian 12 Bookworm or newer).

## 1. Python deps

Stdlib does most of the work. The only external dep is `pyserial`:

```bash
sudo apt update
sudo apt install -y python3 python3-pip python3-serial
# OR via pip in a venv:
python3 -m venv .venv && source .venv/bin/activate
pip install pyserial
```

## 2. USB serial permissions

The Pico A enumerates as `/dev/ttyACM0` (or ACM1 if another USB-CDC
device is plugged in). Add the user to `dialout`:

```bash
sudo usermod -aG dialout $USER
# log out + back in for the group to apply
```

Verify Pico A is visible:

```bash
ls -l /dev/ttyACM*
# crw-rw---- 1 root dialout 166, 0 ... /dev/ttyACM0
```

## 3. Data directory

Default chat DB and live store live at `~/trip_data/`:

```bash
mkdir -p ~/trip_data
```

`hub.py --chat-db <path>` to override. Trip_Hub uses the same
directory for its SQLite + profiles.json — keep them on the same Pi
or symlink.

## 4. Run

```bash
cd ~/repo/esp32_projects/Hub_Server
python3 hub.py
```

Common flags:

| Flag | Effect |
|---|---|
| `--port /dev/ttyACM0` | skip auto-detect |
| `--log picoA_serial.log` | override raw serial log path |
| `--gps-log gps.log` | override GPS-only log path |
| `--server http://localhost:5000` | also POST `GPS:` payloads to Trip_Hub `/api/live_point` |
| `--chat-db /custom/path/trips.db` | override SQLite path |

Special prompts: type `PING`, `RESET`, or `QUIT` (or Ctrl-C) to exit.

## 5. Run on boot (optional)

Systemd unit at `/etc/systemd/system/hub-server.service`:

```ini
[Unit]
Description=Hub_Server LoRa client
After=multi-user.target

[Service]
Type=simple
User=pi
WorkingDirectory=/home/pi/repo/esp32_projects/Hub_Server
ExecStart=/usr/bin/python3 hub.py --server http://localhost:5000
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now hub-server.service
journalctl -u hub-server -f
```
