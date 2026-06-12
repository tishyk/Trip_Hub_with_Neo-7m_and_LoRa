# Hub_Server — Pi 5 hub server (Python 3)

Server-side half of the network hub. Runs on the Raspberry Pi 5 and
talks to [../Hub_Server_Firmware/](../Hub_Server_Firmware/) over USB
serial. Together they implement the Pi 5 ↔ LoRa boundary. The Flask
trip viewer is a separate deployable in
[../Trip_Hub/](../Trip_Hub/).

## Language — fixed
**Python 3 (CPython on Linux).** Stdlib only except for `pyserial`.
Do not introduce frameworks or compiled extensions.

## Modules

| File | Role |
|---|---|
| [hub.py](hub.py) | Main entry. Connects to Pico A on `/dev/ttyACM*`, ships TX commands, receives RX events, owns reconnect loop. |
| [splitter.py](splitter.py) | Routes incoming LoRa payloads. `GPS:` → JSON parse + log + optional POST to Trip_Hub. |
| [chat_db.py](chat_db.py) | SQLite chat persistence (default `~/trip_data/trips.db`). Polls a pending-TX table. |
| [sync_manager.py](sync_manager.py) | Pi-side of the trip sync state machine. Generates `QTRIPS:` / `QTRIP:` / `QPTS:` queries, processes `RTRIPS:` / `RTRIP:` / `RPTS:`, sends `ACK:`. |
| [sync_codec.py](sync_codec.py) | Encode/decode RPTS batched fix packets (delta-compressed JSON). Mirror of the MicroPython side in [../PicoB/sync_codec.py](../PicoB/sync_codec.py). |

## Run

```bash
python3 hub.py                                  # auto-detect Pico
python3 hub.py --port /dev/ttyACM0
python3 hub.py --server http://localhost:5000   # also POST GPS to Trip_Hub
```

See [SETUP_SUMMARY.md](SETUP_SUMMARY.md) for first-time install steps
(USB perms, pyserial, autostart).

## Logs
Runtime logs land in [logs/](logs/):
- `picoA_serial.log` — raw serial traffic + RSSI/SNR
- `gps.log` — JSON-parsed GPS fixes
- `07-08_log.txt` — daily bridge runtime log

## USB-serial contract with the firmware

| Pi 5 → Pico A | Pico A → Pi 5 |
|---|---|
| `TX:<plaintext>` | `READY` |
| `PING` | `RX:<text>\|<rssi>\|<snr>` |
| `RESET` | `LOG:<note>` |
| `TIME:<iso>` | `ERR:<reason>` |
| | `OK` / `PONG` (responses) |

Plaintext only; firmware AES-encrypts before TX and decrypts after RX.
Wire details and bridge routing are in
[../Hub_Server_Firmware/CLAUDE.md](../Hub_Server_Firmware/CLAUDE.md).

For chat, hub.py wraps free text as `TX:CHAT:<text>` so the LoRa
payload is `CHAT:<text>` and remote nodes can route it. GPS-formatted
lines from the `gps lat lon` console shortcut go on the wire as-is.
Pico A strips the `CHAT:` prefix on incoming chat before forwarding to
the Pi as `RX:<text>|...`, so chat_db sees bare user text.

## Status
Proven, in-production. Match the existing module/file split when
adding features.
