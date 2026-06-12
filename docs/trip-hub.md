# Raspberry Pi 5 gateway — Hub_Server + Trip Hub web app

The Pi is the gateway and the dashboard host. Two cooperating Python 3 services:

- **`Hub_Server`** — the LoRa-side router and trip-sync engine (talks to Pico A
  over USB serial). [../Hub_Server/](../Hub_Server/)
- **`Trip_Hub`** — the Flask web app: map, analytics, chat, presence.
  [../Trip_Hub/](../Trip_Hub/)

Both read/write one SQLite file (`trips.db`).

## Hub_Server — the router & sync engine

| File | Role |
|---|---|
| `hub.py` | main entry: owns `/dev/ttyACM*` (Pico A), reconnect loop, RX dispatch, device-presence ingest, console TX |
| `splitter.py` | routes incoming payloads (`GPS:` → log + POST; `TRIP*` → POST) |
| `sync_manager.py` | Pi-side trip-sync state machine (`QTRIPS/QTRIP/QPTS` out, `RTRIPS/RTRIP/RPTS` in, `ACK`) |
| `sync_codec.py` | RPTS delta codec (mirror of the device side) |
| `chat_db.py` | SQLite chat persistence + pending-TX queue |

RX classification order: `DEVICE:` → sync → `GPS:` → `TRIP*` → `CHAT:` → else
(log only). Only `CHAT:` reaches the chat store; `GPS:`/`TRIP*` go to their
pipelines; everything else is logged but never pollutes chat.

```bash
cd Hub_Server
python3 hub.py                                # auto-detect Pico A
python3 hub.py --server http://localhost:5000 # also POST GPS/trips to Trip_Hub
```

## Trip_Hub — the web app

Single-page Flask app + Leaflet. See [web-interfaces.md](web-interfaces.md) for a
screenshot walkthrough.

| File | Role |
|---|---|
| `trip_server.py` | Flask app, SQLite access, all `/api/*` routes |
| `index.html` | the whole UI (map, chat, stats, activity log) — no build step |
| `import_trips.py` | bulk-import `T*.gps`/`.json` dropped into `incoming/trips/` |

Selected API:

| Endpoint | Purpose |
|---|---|
| `GET /` | the dashboard |
| `GET /api/trips` | list trips |
| `GET /api/trip/<id>` | one trip + polyline |
| `POST /api/live_point` | ingest a live `GPS:` fix (from Hub_Server) |
| `POST /api/trip` / `/api/trip_event` | ingest a synced/finished trip |
| `GET /api/devices/presence` | device roster `{name,hwid,last_seen,rssi}` for the chat dots |
| `POST /api/devices/probe` | broadcast a `WHO?` presence probe |
| chat + journeys endpoints | message history, send, journey grouping |

```bash
cd Trip_Hub
python3 trip_server.py     # serves http://<pi-ip>:5000 (binds 0.0.0.0)
```

## Data store (`trips.db`)

`trips`, `trip_points`, `live_points`, `messages`, `devices`
(`id`/`name`/`last_seen`/`last_rssi`), `journeys`, `journey_trips`. Schema +
migrations are created on `trip_server.py` startup. See
[architecture.md → Persistence](architecture.md#persistence-sqlite-tripsdb).

## Running both (example: tmux)

```bash
tmux new-session -d -s hub_server -c .../Hub_Server "python3 hub.py --server http://localhost:5000"
tmux new-session -d -s trip_hub   -c .../Trip_Hub  "python3 trip_server.py"
```
`Hub_Server` POSTs to `Trip_Hub` over local HTTP, so start/restart them
independently — just make sure **both** are up (it’s easy to restart the router
and forget the web app on `:5000`).
