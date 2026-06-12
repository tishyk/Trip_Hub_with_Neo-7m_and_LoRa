#!/usr/bin/env python3
"""
hub.py
------
Pi 5 hub runtime. Talks to the Pico A bridge over USB serial; owns the
LoRa-side end of every protocol the network speaks (chat, GPS ingest,
trip persistence, sync Q/R, device rename / listen / QPOS / WHO?).

Logs all serial traffic to picoA_serial.log (timestamp + RSSI/SNR).
GPS broadcasts with valid lat/lon JSON are also written to gps.log.

Chat messages are persisted to a SQLite database (default:
~/trip_data/trips.db). The Flask web UI reads this DB and shows a chat
panel. Messages typed on the web page are inserted as pending TX rows;
this process polls for them and ships them out the radio.

Auto-reconnects if the Pico is unplugged, reset, or the port disappears.

Usage:
    pip install pyserial   (one time)
    python3 hub.py                                  # auto-detect Pico port
    python3 hub.py --port /dev/ttyACM0
    python3 hub.py --log picoA_serial.log --gps-log gps.log
    python3 hub.py --server http://localhost:5000   # also POST GPS
                                                     # to trip-tracker
    python3 hub.py --chat-db /custom/path/trips.db  # custom DB path

Type a line + Enter -> Pico transmits over LoRa.
Special commands: PING, RESET, QUIT (or Ctrl-C)

Node-side counterpart on PicoB lives in PicoB/runtime.py.
"""

import argparse
import datetime
import json
import os
import sqlite3
import sys
import threading
import time
from pathlib import Path

try:
    import serial
    from serial.tools import list_ports
except ImportError:
    print("ERROR: pyserial not installed. Run:  pip install pyserial")
    sys.exit(1)

import splitter
import chat_db
import sync_manager as sync_mod


PICO_VID_PIDS = [
    (0x2E8A, 0x0005),
    (0x2E8A, 0x000A),
    (0x2E8A, 0x0009),
]

# Hub's identity on the LoRa wire. Every CHAT message we send out gets
# prepended with this so receivers (other devices, Pi DB) can attribute
# messages back to the hub. Matches the auto-PONG sender used by the
# Pico A bridge firmware.
HUB_NAME = "HubServer"


def find_pico_port():
    for p in list_ports.comports():
        if p.vid is None:
            continue
        for vid, pid in PICO_VID_PIDS:
            if p.vid == vid and p.pid == pid:
                return p.device
        desc = (p.description or "") + " " + (p.manufacturer or "")
        if "Pico" in desc or "RP2" in desc or "MicroPython" in desc:
            return p.device
    return None


def now_str():
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def log_line(log_path, level, text):
    line = "{} {} {}\n".format(now_str(), level, text)
    sys.stdout.write(line)
    sys.stdout.flush()
    if log_path:
        try:
            with open(log_path, "a") as f:
                f.write(line)
        except Exception as e:
            sys.stderr.write("log write failed: {}\n".format(e))


def _handle_device_payload(payload, conn, log_path, db_path, rssi=None):
    """Dispatch DEVICE:<json> announces.

    Wire grammar — every payload carries BOTH fields:
      id   = permanent hardware id (RP2040 unique_id / ESP32 chip MAC)
      name = renameable label (lives in device_id.txt on flash)

    The Pi only receives announces. Logic:
      - UPSERT devices(id, name, last_seen).
      - If devices.name was different (i.e. this id was known under
        another label): rename detected. Cascade name change into
        trips / live_points / messages, and mark any matching inflight
        rename op as success.
      - Else (id is brand new): if a 'listen' op is armed, promote it
        to a rename op (now we know the id) and fire the rename. The
        device will reboot and re-announce; that future announce will
        trigger the rename-detection branch above.
      - Else (id known, name unchanged): plain heartbeat — log + bump
        last_seen.
    """
    if not db_path:
        log_line(log_path, "DEV", "DEVICE: with no db_path on connection")
        return
    body = payload[len("DEVICE:"):].strip()
    try:
        obj = json.loads(body)
    except Exception as e:
        log_line(log_path, "DEV",
                 "malformed DEVICE: {} ({})".format(body[:60], e))
        return
    hwid = obj.get("id")
    name = obj.get("name")
    if not hwid or not name:
        log_line(log_path, "DEV", "DEVICE missing id or name: {}".format(body[:60]))
        return

    try:
        sql = sqlite3.connect(db_path)
        c   = sql.cursor()
        now_iso = datetime.datetime.now().isoformat()

        # Look up prior name for this hwid (None on first sighting).
        c.execute("SELECT name FROM devices WHERE id=?", (hwid,))
        row = c.fetchone()
        prior_name = row[0] if row else None

        # UPSERT the device row. last_rssi = signal of this announce as
        # heard by the Pico A bridge (the hub's vantage); the bridge's own
        # self-announce arrives with rssi 0, which the UI treats as "no link".
        c.execute("""INSERT INTO devices(id, name, last_seen, last_rssi)
                     VALUES (?, ?, ?, ?)
                     ON CONFLICT(id) DO UPDATE SET
                         name=excluded.name,
                         last_seen=excluded.last_seen,
                         last_rssi=excluded.last_rssi""",
                  (hwid, name, now_iso, rssi))

        if prior_name is not None and prior_name != name:
            # Rename detected via name change for a known hwid.
            c.execute("UPDATE trips       SET device_id=? WHERE device_id=?",
                      (name, prior_name))
            c.execute("UPDATE messages    SET source=?    WHERE source=?",
                      (name, prior_name))
            c.execute("UPDATE live_points SET source=?    WHERE source=?",
                      (name, prior_name))
            # Mark any matching inflight rename op as success.
            c.execute("""SELECT id, payload FROM pending_lora_ops
                         WHERE op='rename' AND status='inflight'""")
            for row_id, row_payload in c.fetchall():
                try:
                    p = json.loads(row_payload)
                except Exception:
                    continue
                if p.get("id") == hwid and p.get("new") == name:
                    c.execute("""UPDATE pending_lora_ops
                                 SET status='success', completed_at=?
                                 WHERE id=?""", (now_iso, row_id))
                    log_line(log_path, "RNM",
                             "renamed {} -> {} (hwid={}, op {})".format(
                                 prior_name, name, hwid[:8], row_id))
                    break
            else:
                log_line(log_path, "RNM",
                         "renamed {} -> {} (hwid={}, no op)".format(
                             prior_name, name, hwid[:8]))
            sql.commit()
            sql.close()
            return

        if prior_name is None:
            # First sighting. If a listen op is armed, promote it.
            c.execute("""SELECT id, payload FROM pending_lora_ops
                         WHERE op='listen' AND status='inflight'
                         ORDER BY id LIMIT 1""")
            lrow = c.fetchone()
            if lrow is not None:
                op_id, payload_json = lrow
                try:
                    p = json.loads(payload_json)
                except Exception as e:
                    sql.commit(); sql.close()
                    log_line(log_path, "ERR", "listen_payload_parse:{}".format(e))
                    return
                new_name = p.get("new")
                if not new_name:
                    sql.commit(); sql.close()
                    log_line(log_path, "ERR",
                             "listen op {} missing 'new'".format(op_id))
                    return
                # Carry hwid + old name into the rename op so the eventual
                # post-reboot announce can be matched by id+new_name.
                new_payload = json.dumps({"id": hwid, "old": name, "new": new_name})
                c.execute("""UPDATE pending_lora_ops
                             SET op='rename', payload=?, started_at=?
                             WHERE id=?""", (new_payload, now_iso, op_id))
                sql.commit()
                sql.close()

                wire = ('DEVICE:{"id":"' + hwid +
                        '","name":"' + new_name + '"}')
                if conn.write(("TX:" + wire + "\n").encode("utf-8")):
                    log_line(log_path, "DEV",
                             "listen captured hwid={} {} -> {} (op {})".format(
                                 hwid[:8], name, new_name, op_id))
                else:
                    sql = sqlite3.connect(db_path)
                    sql.execute("""UPDATE pending_lora_ops
                                   SET status='failed',
                                       error='USB write failed',
                                       completed_at=?
                                   WHERE id=?""", (now_iso, op_id))
                    sql.commit()
                    sql.close()
                    log_line(log_path, "ERR",
                             "listen rename USB write failed (op {})".format(op_id))
                return
            log_line(log_path, "DEV",
                     "new device hwid={} name={}".format(hwid[:8], name))

        # Heartbeat (known hwid, unchanged name) — devices row last_seen
        # was already bumped by the UPSERT.
        sql.commit()
        sql.close()
    except Exception as e:
        log_line(log_path, "ERR", "device_payload:{}".format(e))


def lora_op_worker_loop(conn, log_path, db_path, stop_evt, poll_interval=1.0):
    """Pump pending_lora_ops:
       - Pick up a 'pending' row, send the LoRa packet, mark 'inflight'.
       - Time out 'inflight' rows past their op-specific window.
       reader_loop's _handle_device_payload is what marks rows 'success'.
    """
    if not db_path:
        return
    while not stop_evt.is_set():
        time.sleep(poll_interval)
        if not conn.is_alive():
            continue
        try:
            sql = sqlite3.connect(db_path)
            c   = sql.cursor()

            # Time out stuck inflight ops, op-specific windows.
            now_dt = datetime.datetime.now()
            now_iso_top = now_dt.isoformat()
            cutoff_rename = (now_dt - datetime.timedelta(seconds=25)).isoformat()
            cutoff_listen = (now_dt - datetime.timedelta(seconds=30)).isoformat()
            c.execute("""UPDATE pending_lora_ops
                         SET status='timeout',
                             completed_at=?,
                             error='no DEVICE announce within 25s'
                         WHERE op='rename' AND status='inflight'
                           AND started_at < ?""",
                      (now_iso_top, cutoff_rename))
            c.execute("""UPDATE pending_lora_ops
                         SET status='timeout',
                             completed_at=?,
                             error='no DEVICE announce within 30s'
                         WHERE op='listen' AND status='inflight'
                           AND started_at < ?""",
                      (now_iso_top, cutoff_listen))
            sql.commit()

            # Pick the oldest pending op.
            c.execute("""SELECT id, op, payload FROM pending_lora_ops
                         WHERE status='pending'
                         ORDER BY id LIMIT 1""")
            row = c.fetchone()
            if not row:
                sql.close()
                continue
            op_id, op_type, payload_json = row
            now_iso = datetime.datetime.now().isoformat()

            if op_type == "rename":
                try:
                    p = json.loads(payload_json)
                    wire = ('DEVICE:{"id":"' + p["id"] +
                            '","name":"' + p["new"] + '"}')
                except Exception as e:
                    c.execute("""UPDATE pending_lora_ops
                                 SET status='failed', error=?, completed_at=?
                                 WHERE id=?""",
                              ("payload parse: " + str(e), now_iso, op_id))
                    sql.commit(); sql.close()
                    continue
                # Mark inflight first so RENAMED arriving early can match.
                c.execute("""UPDATE pending_lora_ops
                             SET status='inflight', started_at=?
                             WHERE id=?""", (now_iso, op_id))
                sql.commit()
                # Send via Pico A USB: TX:<wire>\n
                if conn.write(("TX:" + wire + "\n").encode("utf-8")):
                    log_line(log_path, "RNM",
                             "sent {} (op {})".format(wire, op_id))
                else:
                    c.execute("""UPDATE pending_lora_ops
                                 SET status='failed',
                                     error='USB write failed',
                                     completed_at=?
                                 WHERE id=?""", (now_iso, op_id))
                    sql.commit()
            elif op_type == "listen":
                # No LoRa packet to send — just arm the row (mark
                # inflight, stamp started_at) and let reader_loop's
                # _handle_device_payload promote it on the next announce.
                # The 30-s timeout above ends the window if no device
                # announces.
                c.execute("""UPDATE pending_lora_ops
                             SET status='inflight', started_at=?
                             WHERE id=?""", (now_iso, op_id))
                sql.commit()
                log_line(log_path, "DEV",
                         "listen armed (op {})".format(op_id))
            elif op_type == "qpos":
                # Fire-and-forget position query. The device's reply is
                # a normal GPS: broadcast that flows through the existing
                # live-points pipeline — no Pi-side state to track. We
                # mark 'success' as soon as the wire-write returns ok.
                try:
                    p = json.loads(payload_json)
                    wire = "QPOS:" + p["id"]
                except Exception as e:
                    c.execute("""UPDATE pending_lora_ops
                                 SET status='failed', error=?, completed_at=?
                                 WHERE id=?""",
                              ("payload parse: " + str(e), now_iso, op_id))
                    sql.commit(); sql.close()
                    continue
                if conn.write(("TX:" + wire + "\n").encode("utf-8")):
                    c.execute("""UPDATE pending_lora_ops
                                 SET status='success', started_at=?,
                                     completed_at=?
                                 WHERE id=?""", (now_iso, now_iso, op_id))
                    log_line(log_path, "QPS",
                             "sent {} (op {})".format(wire, op_id))
                else:
                    c.execute("""UPDATE pending_lora_ops
                                 SET status='failed',
                                     error='USB write failed',
                                     completed_at=?
                                 WHERE id=?""", (now_iso, op_id))
                sql.commit()
            elif op_type == "probe":
                # Broadcast presence probe. Every device in range replies
                # with its boot-style DEVICE: announce, which bumps
                # devices.last_seen via the existing _handle_device_payload
                # path. No payload state to track on the Pi side.
                wire = "WHO?"
                if conn.write(("TX:" + wire + "\n").encode("utf-8")):
                    c.execute("""UPDATE pending_lora_ops
                                 SET status='success', started_at=?,
                                     completed_at=?
                                 WHERE id=?""", (now_iso, now_iso, op_id))
                    log_line(log_path, "WHO",
                             "broadcast {} (op {})".format(wire, op_id))
                else:
                    c.execute("""UPDATE pending_lora_ops
                                 SET status='failed',
                                     error='USB write failed',
                                     completed_at=?
                                 WHERE id=?""", (now_iso, op_id))
                sql.commit()
            elif op_type == "sync_pull":
                # User-triggered sync: synthesise a SYNC:<hwid> as if it
                # had arrived from the device. SyncManager's normal
                # on_message path sets up the session and TXes QTRIPS,
                # which PicoB responds to with RTRIPS exactly like an
                # organic sync. Skips PicoB's 5-min SYNC_RETRY_MS wait.
                try:
                    p = json.loads(payload_json)
                    hwid = p["id"]
                except Exception as e:
                    c.execute("""UPDATE pending_lora_ops
                                 SET status='failed', error=?, completed_at=?
                                 WHERE id=?""",
                              ("payload parse: " + str(e), now_iso, op_id))
                    sql.commit(); sql.close()
                    continue
                sync_mgr = getattr(conn, "sync_mgr", None)
                if sync_mgr is None:
                    c.execute("""UPDATE pending_lora_ops
                                 SET status='failed', error='no sync_mgr',
                                     completed_at=?
                                 WHERE id=?""", (now_iso, op_id))
                else:
                    try:
                        # Force-reset the session before injecting the
                        # synthetic SYNC. The normal _on_sync path refuses
                        # to send QTRIPS while a session has current_trip
                        # set (protects against re-announce wiping mid-
                        # flight state); a user-pull is an explicit reset
                        # request so we wipe the state ourselves first.
                        sess = sync_mgr._session(hwid)
                        sess.reset()
                        sync_mgr._qtrips_pending.pop(hwid, None)
                        sync_mgr.on_message("SYNC:" + hwid)
                        c.execute("""UPDATE pending_lora_ops
                                     SET status='success', started_at=?,
                                         completed_at=?
                                     WHERE id=?""",
                                  (now_iso, now_iso, op_id))
                        log_line(log_path, "SYN",
                                 "user-pull SYNC:{} (op {})".format(hwid, op_id))
                    except Exception as e:
                        c.execute("""UPDATE pending_lora_ops
                                     SET status='failed', error=?,
                                         completed_at=?
                                     WHERE id=?""",
                                  (str(e), now_iso, op_id))
                sql.commit()
            else:
                c.execute("""UPDATE pending_lora_ops
                             SET status='failed',
                                 error='unknown op',
                                 completed_at=?
                             WHERE id=?""", (now_iso, op_id))
                sql.commit()
            sql.close()
        except Exception as e:
            log_line(log_path, "ERR", "lora_op_worker:{}".format(e))


def parse_rx(payload):
    """Parse 'text|rssi|snr' from an RX: line."""
    parts = payload.rsplit("|", 2)
    if len(parts) != 3:
        return payload, None, None
    try:
        return parts[0], int(parts[1]), float(parts[2])
    except ValueError:
        return payload, None, None


# ============================================================
# Connection - manages a serial port with auto-reconnect.
#
# We share a single Connection between the reader thread and the main
# thread. Both grab the lock when reading/writing. If a read or write
# fails, we mark the connection as dead. The main thread's reconnect
# loop notices and re-opens.
# ============================================================
class Connection:
    def __init__(self, requested_port, baud, log_path, gps_log_path):
        self.requested_port = requested_port  # may be None (auto-detect)
        self.baud = baud
        self.log_path = log_path
        self.gps_log_path = gps_log_path
        self.ser = None
        self.port = None
        self.lock = threading.Lock()
        self.alive = False  # True when serial port is open and usable

    def is_alive(self):
        if not self.alive or self.ser is None:
            return False
        # Best-effort liveness check: device file still exists?
        if self.port and not os.path.exists(self.port):
            self.alive = False
            return False
        return True

    def open(self):
        """(Re)open the serial port. Tries until successful or interrupted."""
        first_try = True
        while True:
            port = self.requested_port or find_pico_port()
            if port and os.path.exists(port):
                try:
                    ser = serial.Serial(port, self.baud, timeout=0.2)
                    time.sleep(0.2)
                    ser.reset_input_buffer()
                    with self.lock:
                        self.ser = ser
                        self.port = port
                        self.alive = True
                    log_line(self.log_path, "SYS",
                             "connected port={}".format(port))
                    return
                except Exception as e:
                    if first_try:
                        log_line(self.log_path, "ERR",
                                 "open_failed:{}".format(e))
                        first_try = False
            else:
                if first_try:
                    log_line(self.log_path, "SYS",
                             "waiting_for_pico ...")
                    first_try = False
            time.sleep(1.0)

    def close(self):
        """Close the current port if any."""
        with self.lock:
            self.alive = False
            if self.ser is not None:
                try:
                    self.ser.close()
                except Exception:
                    pass
                self.ser = None

    def write(self, data):
        """Write bytes. Marks connection dead on failure (caller will reconnect)."""
        with self.lock:
            if not self.alive or self.ser is None:
                return False
            try:
                self.ser.write(data)
                return True
            except Exception as e:
                log_line(self.log_path, "ERR",
                         "write_failed:{}".format(e))
                self.alive = False
                try:
                    self.ser.close()
                except Exception:
                    pass
                self.ser = None
                return False

    def read(self, n):
        """Read up to n bytes. Returns b'' on timeout, None on failure."""
        with self.lock:
            if not self.alive or self.ser is None:
                return None
            ser = self.ser
        try:
            return ser.read(n)
        except Exception as e:
            log_line(self.log_path, "ERR",
                     "read_failed:{}".format(e))
            with self.lock:
                self.alive = False
                if self.ser is not None:
                    try:
                        self.ser.close()
                    except Exception:
                        pass
                    self.ser = None
            return None


def _safe_sync_on_message(sync_mgr, payload, log_path):
    """Belt-and-suspenders wrapper. The reader thread used to crash on
    rare SyncManager exceptions (e.g. duplicate RPTS after ACK popped
    the trip), which froze all incoming LoRa traffic until manual hub
    restart. Catch + log here so the thread stays alive."""
    try:
        return sync_mgr.on_message(payload)
    except Exception as e:
        log_line(log_path, "ERR", "sync_on_message:{}".format(e))
        return False


def reader_loop(conn, stop_evt, on_ready=None):
    """Read lines from the connection, parse, and log them.
    On read failure, just spin and wait for the connection to come back
    (Connection.read returns None when dead).

    on_ready: optional callback invoked when a READY line is seen from Pico.
              Used to auto-send TIME sync on every (re)connect.
    """
    buf = b""
    while not stop_evt.is_set():
        if not conn.is_alive():
            time.sleep(0.2)
            continue
        chunk = conn.read(128)
        if chunk is None:
            # Connection died - main thread will reconnect. Drop the buffer.
            buf = b""
            time.sleep(0.2)
            continue
        if not chunk:
            continue
        buf += chunk
        while b"\n" in buf:
            line, buf = buf.split(b"\n", 1)
            text = line.decode("utf-8", errors="replace").strip()
            if not text:
                continue
            if text.startswith("RX:"):
                payload, rssi, snr = parse_rx(text[3:])
                meta = ""
                if rssi is not None and snr is not None:
                    meta = "  [RSSI={} SNR={:.1f}]".format(rssi, snr)
                log_line(conn.log_path, "RX ", payload + meta)

                # DEVICE: umbrella for boot announce + rename ack. Single
                # JSON-routed handler — no other layer should see these.
                if payload.startswith("DEVICE:"):
                    _handle_device_payload(
                        payload, conn,
                        log_path=conn.log_path,
                        db_path=getattr(conn, "chat_db_path", None),
                        rssi=rssi)
                # Sync protocol messages - handle first, don't route elsewhere
                elif (sync_mgr := getattr(conn, "sync_mgr", None)) is not None \
                        and _safe_sync_on_message(sync_mgr, payload, conn.log_path):
                    log_line(conn.log_path, "SYN", payload[:60])

                # Route GPS payloads to gps.log (splitter handles parsing)
                elif splitter.is_gps(payload):
                    on_post = getattr(conn, "gps_poster", None)
                    obj = splitter.handle_gps(
                        payload, conn.gps_log_path,
                        rssi=rssi, snr=snr, on_post=on_post)
                    if obj is None:
                        log_line(conn.log_path, "ERR",
                                 "gps_parse_failed:{}".format(payload))
                    else:
                        log_line(conn.log_path, "GPS",
                                 "lat={} lon={}".format(obj["lat"], obj["lon"]))
                elif splitter.is_trip(payload):
                    # TRIPSTART / TRIPEND - forward to server via trip_poster
                    trip_poster = getattr(conn, "trip_poster", None)
                    kind, obj = splitter.handle_trip(
                        payload, on_post=trip_poster,
                        log_callback=lambda lvl, txt: log_line(conn.log_path, lvl, txt))
                    if kind is None:
                        log_line(conn.log_path, "ERR",
                                 "trip_parse_failed:{}".format(payload))
                    else:
                        dev = obj.get('device_id') or obj.get('d') or '?'
                        log_line(conn.log_path, "TRIP",
                                 "{} device={}".format(kind, dev))
                elif payload.startswith("CHAT:"):
                    # Wire format: CHAT:<sender_name>:<body>. Bridge
                    # keeps the prefix so we can attribute. Fall back to
                    # 'picoB' if the sender is missing for any reason
                    # (legacy traffic, malformed packet).
                    rest = payload[5:]
                    colon = rest.find(":")
                    if colon > 0:
                        sender = rest[:colon]
                        body   = rest[colon+1:]
                    else:
                        sender = 'picoB'
                        body   = rest
                    chat_db_inst = getattr(conn, "chat", None)
                    if chat_db_inst is not None:
                        try:
                            chat_db_inst.add_rx(body, rssi=rssi, snr=snr,
                                                source=sender)
                        except Exception as e:
                            log_line(conn.log_path, "ERR",
                                     "chat_db_rx_failed:{}".format(e))
                else:
                    # Untagged payload — log only, don't pollute chat.
                    log_line(conn.log_path, "???", payload[:80])
            elif text.startswith("LOG:"):
                log_line(conn.log_path, "LOG", text[4:])
            elif text.startswith("ERR:"):
                log_line(conn.log_path, "ERR", text[4:])
            elif text == "READY":
                log_line(conn.log_path, "SYS", "READY")
                if on_ready:
                    try:
                        on_ready()
                    except Exception as e:
                        log_line(conn.log_path, "ERR",
                                 "on_ready_failed:{}".format(e))
            elif text == "PONG":
                log_line(conn.log_path, "SYS", "PONG")
            elif text == "OK":
                log_line(conn.log_path, "SYS", "OK")
            else:
                log_line(conn.log_path, "???", text)


def reconnect_loop(conn, stop_evt):
    """Watch the connection. If it dies, try to reopen until successful."""
    while not stop_evt.is_set():
        if not conn.is_alive():
            log_line(conn.log_path, "SYS", "reconnecting...")
            conn.close()
            # open() blocks until successful or interrupted by stop_evt
            # We poll stop_evt via short sleeps inside open()
            try:
                conn.open()
            except Exception as e:
                log_line(conn.log_path, "ERR",
                         "reconnect_failed:{}".format(e))
                time.sleep(1.0)
        time.sleep(0.5)


def tx_worker_loop(conn, chat, log_path, stop_evt, poll_interval=0.5):
    """Poll the chat DB for pending TX messages (queued by the web server),
    write them out the USB serial, mark them sent.

    One message per loop iteration so we don't flood the radio. The Pico's
    bridge serializes TX anyway; spacing things ~half a second is plenty.

    If conn isn't alive, just wait. Pending rows stay in the DB until
    we can ship them.
    """
    if chat is None:
        return  # no DB, no work
    while not stop_evt.is_set():
        time.sleep(poll_interval)
        if not conn.is_alive():
            continue
        try:
            pending = chat.get_pending_tx(limit=1)
        except Exception as e:
            log_line(log_path, "ERR", "tx_worker_db:{}".format(e))
            continue
        if not pending:
            continue
        row_id, text, source = pending[0]
        # Wire format: CHAT:<sender>:<body>. Reserve up to 16 B for the
        # sender + ':' separator on top of CHAT: (5 B) plus AES+PKCS7
        # padding inside the 250 B LoRa packet cap → 204 B body.
        if len(text.encode("utf-8")) > 204:
            try:
                chat.mark_failed(row_id, "text_too_long")
            except Exception:
                pass
            log_line(log_path, "ERR",
                     "tx_worker_too_long:id={}".format(row_id))
            continue
        payload = ("TX:CHAT:" + HUB_NAME + ":" + text + "\n").encode("utf-8")
        if conn.write(payload):
            log_line(log_path, "TX ",
                     "from_{}:{}".format(source or "web", text))
            try:
                chat.mark_sent(row_id)
            except Exception as e:
                log_line(log_path, "ERR",
                         "tx_worker_mark_sent:{}".format(e))
        else:
            # write_dropped - leave as pending so reconnect retries.
            log_line(log_path, "WRN",
                     "tx_worker_write_failed:id={}".format(row_id))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", help="serial port (auto-detect if omitted)")
    ap.add_argument("--baud", type=int, default=115200)
    ap.add_argument("--log",  default="picoA_serial.log",
                    help="serial log file path (default: picoA_serial.log)")
    ap.add_argument("--gps-log", default="gps.log",
                    help="GPS log file path (default: gps.log)")
    ap.add_argument("--server", default=None,
                    help="optional URL of trip-tracker server, e.g. "
                         "http://localhost:5000 - if set, GPS points are "
                         "POSTed there in addition to gps.log")
    # Default DB lives in the sibling Trip_Hub/ directory so Hub_Server
    # and Trip_Hub share the canonical SQLite file. Override with --chat-db
    # if running outside the workspace layout.
    _SCRIPT_DIR = Path(__file__).resolve().parent
    _DEFAULT_DB = _SCRIPT_DIR.parent / "Trip_Hub" / "trips.db"
    ap.add_argument("--chat-db",
                    default=str(_DEFAULT_DB),
                    help="path to chat SQLite DB "
                         "(default: ../Trip_Hub/trips.db, same file as Trip_Hub)")
    args = ap.parse_args()

    # Chat DB - shared with the Flask server (same file)
    try:
        chat = chat_db.ChatDB(args.chat_db)
        log_line(args.log, "SYS", "chat_db={}".format(args.chat_db))
    except Exception as e:
        log_line(args.log, "ERR", "chat_db_open_failed:{}".format(e))
        chat = None

    conn = Connection(args.port, args.baud, args.log, args.gps_log)

    # Create the GPS server poster if a server URL was given. Done up here
    # so the reader thread can use it as on_post.
    if args.server:
        gps_poster = splitter.make_server_poster(
            args.server,
            log_callback=lambda level, text: log_line(args.log, level, text))
        log_line(args.log, "SYS", "gps_server={}".format(args.server))
        trip_poster = splitter.make_trip_poster(
            args.server,
            log_callback=lambda level, text: log_line(args.log, level, text))
        log_line(args.log, "SYS", "trip_server={}".format(args.server))
    else:
        gps_poster = None
        trip_poster = None
    # Make poster reachable from reader_loop via Connection
    conn.gps_poster  = gps_poster
    conn.trip_poster = trip_poster
    conn.chat        = chat

    # Sync manager - handles Q/R protocol when Pico B announces new data.
    # send_fn writes a TX: command to the Pico A serial port.
    def _sync_send(text):
        """Encrypt and forward a Q* message to Pico B via Pico A."""
        line = ("TX:" + text + "\n").encode()
        ok = conn.write(line)
        log_line(args.log, "SYN", "TX  " + text[:60])
        return ok

    # Derive profiles + deleted-trips paths from the configured chat-db
    # location so all data files stay co-located. sync_manager reads
    # deleted_trips.json from the directory containing db_path.
    _db_dir = os.path.dirname(os.path.abspath(args.chat_db))
    conn.sync_mgr = sync_mod.SyncManager(
        db            = type('DB', (), {'db_path': args.chat_db})(),
        send_fn       = _sync_send,
        profiles_path = os.path.join(_db_dir, "profiles.json"),
    )

    log_line(args.log, "SYS", "client_started")

    # Initial connect (blocks until Pico is available)
    print("Connecting to Pico (auto-detecting port)...")
    conn.open()

    stop_evt = threading.Event()

    # When Pico A signals READY, push our local time so its DS1302 is correct.
    # Uses Pi 5's OS local time (option a) - if Pi 5 is set to Europe/Prague,
    # the Pico gets Prague time including DST automatically.
    def send_time_sync():
        local = datetime.datetime.now()
        ts = local.strftime("%Y-%m-%dT%H:%M:%S")
        if conn.write(("TIME:" + ts + "\n").encode("utf-8")):
            log_line(args.log, "TX ", "TIME_SYNC:" + ts)
        else:
            log_line(args.log, "ERR", "time_sync_write_failed")

    # Expose the DB path to reader_loop so the DEVICE: handlers (which
    # run in that thread) can run rename + listen-op SQL.
    conn.chat_db_path = args.chat_db

    # Drives SyncManager's retry timers. Without this the Q* messages
    # (QTRIPS / QTRIP / QPTS) never retry on packet loss — a single
    # dropped reply from PicoB silently stalls the sync session until
    # PicoB's own 5-min SYNC re-announce. sync_manager.tick() handles
    # the actual timeout + retry bookkeeping; we just call it every 5 s.
    def sync_tick_loop():
        while not stop_evt.is_set():
            time.sleep(5.0)
            try:
                conn.sync_mgr.tick()
            except Exception as e:
                log_line(args.log, "ERR", "sync_tick:{}".format(e))

    reader = threading.Thread(target=reader_loop,
                              args=(conn, stop_evt),
                              kwargs={"on_ready": send_time_sync},
                              daemon=True)
    reconn = threading.Thread(target=reconnect_loop,
                              args=(conn, stop_evt), daemon=True)
    txwork = threading.Thread(target=tx_worker_loop,
                              args=(conn, chat, args.log, stop_evt),
                              daemon=True)
    opwork = threading.Thread(target=lora_op_worker_loop,
                              args=(conn, args.log, args.chat_db, stop_evt),
                              daemon=True)
    synctick = threading.Thread(target=sync_tick_loop, daemon=True)
    reader.start()
    reconn.start()
    txwork.start()
    opwork.start()
    synctick.start()

    print("\nType a line + Enter to send via LoRa.")
    print("Special commands: PING, RESET, QUIT")
    print("Press Ctrl-C to exit.\n")

    try:
        while True:
            try:
                line = sys.stdin.readline()
            except KeyboardInterrupt:
                break
            if not line:
                break
            line = line.rstrip("\n")
            if not line:
                continue
            cmd_upper = line.strip().upper()
            if cmd_upper == "QUIT":
                break

            if cmd_upper in ("PING", "RESET"):
                payload = (cmd_upper + "\n").encode("utf-8")
                tag = "USB:" + cmd_upper
                is_chat = False
            else:
                # Lines already carrying a known protocol prefix (GPS:,
                # SYNC:, Q*:, R*:, ACK:, TRIP*:, CHAT:) go on the wire
                # verbatim — useful for manual sync-protocol testing from
                # the console. Free text gets wrapped with CHAT: so
                # receivers route it correctly.
                if splitter.has_protocol_prefix(line):
                    wire = line
                else:
                    wire = "CHAT:" + HUB_NAME + ":" + line
                payload = ("TX:" + wire + "\n").encode("utf-8")
                tag = line
                # Only true free-text chat goes to chat_db.
                is_chat = not splitter.has_protocol_prefix(line)

            # Write, retry on transient failures (reconnect_loop will re-open)
            if not conn.is_alive():
                log_line(args.log, "WRN",
                         "queued_while_disconnected:{}".format(tag))
                # Wait briefly for reconnect, then retry once
                for _ in range(20):  # up to ~2s
                    if conn.is_alive():
                        break
                    time.sleep(0.1)

            if conn.write(payload):
                log_line(args.log, "TX ", tag)
                # Persist to chat DB if this was a real text message
                if is_chat and chat is not None:
                    try:
                        chat.add_tx_already_sent(line, source='console')
                    except Exception as e:
                        log_line(args.log, "ERR",
                                 "chat_db_tx_failed:{}".format(e))
            else:
                log_line(args.log, "ERR",
                         "write_dropped:{}".format(tag))
    except KeyboardInterrupt:
        pass

    log_line(args.log, "SYS", "client_stopping")
    stop_evt.set()
    conn.close()
    time.sleep(0.3)


if __name__ == "__main__":
    main()