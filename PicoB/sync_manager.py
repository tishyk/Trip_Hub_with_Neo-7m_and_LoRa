"""
sync_manager.py - Pi 5 side of the LoRa Q/R sync protocol.

Protocol flow (Pico B initiates):
    Pico B -> Pi 5:  SYNC:B1
    Pi 5   -> Pico B: QTRIPS:B1
    Pico B -> Pi 5:  RTRIPS:B1:T123:72,T456:45
    Pi 5   -> Pico B: QTRIP:T123
    Pico B -> Pi 5:  RTRIP:T123:{json_meta}
    Pi 5   -> Pico B: QPTS:T123:0:11
    Pico B -> Pi 5:  RPTS:T123:0:[[...],[...],...]
    Pi 5   -> Pico B: QPTS:T123:11:11
    Pico B -> Pi 5:  RPTS:T123:11:[...]
    ...
    Pi 5   -> Pico B: ACK:T123
    (repeat for T456)

Pi 5 drives data transfer after initial SYNC.
Pico B marks trip SYNC_CONFIRMED on ACK.
If Pi 5 already has the trip, it replaces trip_points with the full set
from Pico B (which is filtered/dense) and updates the trip metadata.

Usage (from hub.py):
    mgr = SyncManager(db, send_fn, profiles_path)
    # On incoming RX line:
    mgr.on_message(line)   # returns True if consumed
"""

import json
import os
import sqlite3
import math
from datetime import datetime


# How many fixes to request per QPTS batch.
# Set to match sync_codec.MAX_PACKET_BYTES capacity (~11 fixes).
BATCH_SIZE = 11


def _to_iso(epoch_ts):
    if epoch_ts is None:
        return None
    try:
        return datetime.fromtimestamp(int(epoch_ts)).isoformat()
    except Exception:
        return None


def _approx_distance_km(lat1, lon1, lat2, lon2):
    avg_lat_rad = (lat1 + lat2) * 0.5 * 0.0174532925
    cos_lat = math.cos(avg_lat_rad)
    dlat = (lat2 - lat1) * 111320.0
    dlon = (lon2 - lon1) * 111320.0 * cos_lat
    return math.sqrt(dlat*dlat + dlon*dlon) / 1000.0


def _load_profiles(profiles_path):
    """Return {type: profile_id} for auto_assign profiles."""
    try:
        with open(profiles_path) as f:
            data = json.load(f)
        profiles = data.get("profiles") if isinstance(data, dict) else data
        out = {}
        for p in (profiles or []):
            if p.get("auto_assign") is True:
                out[p.get("type")] = p.get("id")
        return out
    except Exception:
        return {}


def decode_rpts(encoded_str):
    """Decode RPTS payload into list of absolute [ts,lat,lon,alt,spd]."""
    try:
        data = json.loads(encoded_str)
    except Exception:
        return []
    if not data or not isinstance(data, list):
        return []
    out = []
    prev = None
    for item in data:
        if not isinstance(item, list) or len(item) < 5:
            continue
        if prev is None:
            ts, lat, lon = item[0], item[1], item[2]
            alt, spd = float(item[3]), float(item[4])
        else:
            ts  = prev[0] + item[0]
            lat = round(prev[1] + item[1] / 100000.0, 7)
            lon = round(prev[2] + item[2] / 100000.0, 7)
            alt = float(prev[3] + item[3])
            spd = round(prev[4] + item[4] / 10.0, 3)
        out.append([ts, lat, lon, alt, spd])
        prev = (ts, lat, lon, alt, spd)
    return out


class SyncSession:
    """Tracks the sync state for one device (e.g. "B1") during a sync run."""

    def __init__(self):
        self.pending_trips = []     # trip_ids to sync, in order
        self.current_trip  = None   # trip_id being synced now
        self.expected_from = 0      # next fix index we're waiting for
        self.total_pts     = 0      # total fixes in current trip
        self.meta          = None   # trip metadata dict
        self.points        = []     # accumulated decoded fixes for current trip
        self.db_trip_id    = None   # Pi 5 DB integer id for current trip

    def reset(self):
        self.pending_trips = []
        self.current_trip  = None
        self.expected_from = 0
        self.total_pts     = 0
        self.meta          = None
        self.points        = []
        self.db_trip_id    = None


class SyncManager:
    """Handles the Pi 5 side of the sync protocol.

    db            -- TripDatabase instance (from receiver_pi5_advanced)
    send_fn       -- callable(text) that encrypts and sends over LoRa
    profiles_path -- path to profiles.json for auto-assign
    """

    def __init__(self, db, send_fn, profiles_path=None):
        self.db            = db
        self.send          = send_fn
        self.profiles_path = profiles_path or os.path.expanduser(
            "~/trip_data/profiles.json")
        self._sessions     = {}   # device_id -> SyncSession
        self._profiles     = None  # lazy-loaded

    def _get_profiles(self):
        if self._profiles is None:
            self._profiles = _load_profiles(self.profiles_path)
        return self._profiles

    def _session(self, device_id):
        if device_id not in self._sessions:
            self._sessions[device_id] = SyncSession()
        return self._sessions[device_id]

    def on_message(self, text):
        """Handle one incoming message text.

        Returns True if the message was a sync protocol message and was
        handled here.  Returns False if it should be processed elsewhere.
        """
        text = text.strip()

        if text.startswith("SYNC:"):
            return self._on_sync(text[5:].strip())

        if text.startswith("RTRIPS:"):
            return self._on_rtrips(text[7:])

        if text.startswith("RTRIP:"):
            return self._on_rtrip(text[6:])

        if text.startswith("RPTS:"):
            return self._on_rpts(text[5:])

        return False

    # ---- protocol handlers -----------------------------------------------

    def _on_sync(self, device_id):
        """Pico B announces it has unsent data."""
        print("[SYNC] SYNC from {}".format(device_id))
        sess = self._session(device_id)
        sess.reset()
        msg = "QTRIPS:{}".format(device_id)
        self.send(msg)
        print("[SYNC] -> {}".format(msg))
        return True

    def _on_rtrips(self, payload):
        """RTRIPS:B1:T123:72,T456:45"""
        parts = payload.split(":", 1)
        if len(parts) != 2:
            return True
        device_id, trips_str = parts
        sess = self._session(device_id)
        sess.reset()

        if not trips_str.strip():
            print("[SYNC] No trips to sync from {}".format(device_id))
            return True

        # Parse "T123:72,T456:45"
        trip_pairs = []
        for item in trips_str.split(","):
            item = item.strip()
            if not item:
                continue
            kv = item.split(":")
            if len(kv) == 2:
                trip_pairs.append((kv[0], int(kv[1])))
            else:
                trip_pairs.append((kv[0], 0))

        print("[SYNC] Got {} trip(s) from {}: {}".format(
            len(trip_pairs), device_id,
            [(t, n) for t, n in trip_pairs]))

        sess.pending_trips = [t for t, n in trip_pairs]
        # Store npts in session for later use
        sess._npts_map = {t: n for t, n in trip_pairs}

        # Request first trip
        self._request_next_trip(device_id)
        return True

    def _request_next_trip(self, device_id):
        sess = self._session(device_id)
        if not sess.pending_trips:
            print("[SYNC] All trips synced from {}".format(device_id))
            return
        trip_id = sess.pending_trips[0]
        sess.current_trip = trip_id
        sess.expected_from = 0
        sess.total_pts = getattr(sess, '_npts_map', {}).get(trip_id, 0)
        sess.meta = None
        sess.points = []
        sess.db_trip_id = None
        msg = "QTRIP:{}".format(trip_id)
        self.send(msg)
        print("[SYNC] -> {} (expecting {} pts)".format(msg, sess.total_pts))

    def _on_rtrip(self, payload):
        """RTRIP:T123:{json_meta}"""
        colon = payload.find(":")
        if colon < 0:
            return True
        trip_id  = payload[:colon]
        meta_str = payload[colon+1:]
        try:
            meta = json.loads(meta_str)
        except Exception as e:
            print("[SYNC] RTRIP parse error:", e)
            return True

        # Find which device this belongs to
        device_id = meta.get("device", "B1")
        sess = self._session(device_id)
        if sess.current_trip != trip_id:
            print("[SYNC] RTRIP: unexpected trip_id {}".format(trip_id))
            return True

        sess.meta = meta
        print("[SYNC] Got meta for {} type={} km={} pts={}".format(
            trip_id, meta.get("type"), meta.get("km"), sess.total_pts))

        # Request first batch of fixes
        count = min(BATCH_SIZE, max(sess.total_pts, BATCH_SIZE))
        msg = "QPTS:{}:{}:{}".format(trip_id, 0, count)
        self.send(msg)
        print("[SYNC] -> {}".format(msg))
        return True

    def _on_rpts(self, payload):
        """RPTS:T123:0:[[...],...]"""
        parts = payload.split(":", 2)
        if len(parts) != 3:
            return True
        trip_id, from_s, encoded = parts
        try:
            from_idx = int(from_s)
        except ValueError:
            return True

        # Find session
        device_id = None
        for dev, sess in self._sessions.items():
            if sess.current_trip == trip_id:
                device_id = dev
                break
        if device_id is None:
            print("[SYNC] RPTS: no session for trip {}".format(trip_id))
            return True

        sess = self._session(device_id)

        # Decode fixes
        fixes = decode_rpts(encoded)
        print("[SYNC] RPTS {} fixes from idx {}".format(len(fixes), from_idx))

        sess.points.extend(fixes)
        next_from = from_idx + len(fixes)

        # Are we done? (empty batch OR received all expected)
        done = (not fixes or
                (sess.total_pts > 0 and len(sess.points) >= sess.total_pts))

        if not done:
            # Request next batch
            count = min(BATCH_SIZE, sess.total_pts - next_from
                        if sess.total_pts > 0 else BATCH_SIZE)
            if count <= 0:
                done = True
            else:
                msg = "QPTS:{}:{}:{}".format(trip_id, next_from, count)
                self.send(msg)
                print("[SYNC] -> {}".format(msg))

        if done:
            self._store_trip(device_id, trip_id)
            # Send ACK - Pico B marks as confirmed on receive
            msg = "ACK:{}".format(trip_id)
            self.send(msg)
            print("[SYNC] -> {} ({} pts stored)".format(msg, len(sess.points)))
            # Move to next trip
            sess.pending_trips.pop(0)
            self._request_next_trip(device_id)

        return True

    def _store_trip(self, device_id, trip_id):
        """Write a completed sync'd trip to the Pi 5 database."""
        sess = self._session(device_id)
        meta   = sess.meta or {}
        points = sess.points

        movement_type = meta.get("type") or "unknown"
        profiles      = self._get_profiles()
        profile_id    = profiles.get(movement_type)

        conn = sqlite3.connect(self.db.db_path)
        c    = conn.cursor()

        # Find existing trip row if any (match by device + start_time)
        start_iso = _to_iso(meta.get("start_ts"))
        existing_id = None
        if start_iso and device_id:
            c.execute(
                "SELECT id FROM trips WHERE device_id=? AND start_time=? LIMIT 1",
                (device_id, start_iso))
            row = c.fetchone()
            if row:
                existing_id = row[0]

        if existing_id:
            # Update existing trip metadata (Pico B is authoritative)
            c.execute('''UPDATE trips SET
                end_time=?, start_lat=?, start_lon=?, end_lat=?, end_lon=?,
                distance_km=?, duration_seconds=?, movement_type=?,
                avg_speed_kmh=?, max_speed_kmh=?, sync_status=?
                WHERE id=?''',
                (_to_iso(meta.get("end_ts")),
                 meta.get("start_lat"), meta.get("start_lon"),
                 meta.get("end_lat"),   meta.get("end_lon"),
                 meta.get("km"),        meta.get("dur"),
                 movement_type,
                 meta.get("avg"),       meta.get("max"),
                 "synced", existing_id))
            db_id = existing_id
            # Delete existing points (we'll replace with full set from Pico B)
            c.execute("DELETE FROM trip_points WHERE trip_id=?", (db_id,))
            print("[SYNC] Updated existing trip DB id={}".format(db_id))
        else:
            # Insert new trip
            c.execute('''INSERT INTO trips
                (start_time, end_time, start_lat, start_lon, end_lat, end_lon,
                 distance_km, duration_seconds, movement_type, profile_id,
                 received_time, sync_status, manual_classification,
                 avg_speed_kmh, max_speed_kmh, device_id)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                (_to_iso(meta.get("start_ts")),
                 _to_iso(meta.get("end_ts")),
                 meta.get("start_lat"), meta.get("start_lon"),
                 meta.get("end_lat"),   meta.get("end_lon"),
                 meta.get("km"),        meta.get("dur"),
                 movement_type, profile_id,
                 datetime.now().isoformat(), "synced", 0,
                 meta.get("avg"),       meta.get("max"),
                 device_id))
            db_id = c.lastrowid
            print("[SYNC] Inserted new trip DB id={}".format(db_id))

        # Insert all points
        prev_lat = prev_lon = None
        cumulative_km = 0.0
        for fix in points:
            if len(fix) < 5:
                continue
            ts, lat, lon = fix[0], fix[1], fix[2]
            spd = fix[4] if fix[4] is not None else 0.0
            if prev_lat is not None:
                cumulative_km += _approx_distance_km(prev_lat, prev_lon, lat, lon)
            prev_lat, prev_lon = lat, lon
            c.execute('''INSERT INTO trip_points
                (trip_id, latitude, longitude, timestamp, distance_km, speed_kmh, device_id)
                VALUES (?,?,?,?,?,?,?)''',
                (db_id, lat, lon, _to_iso(ts),
                 round(cumulative_km, 4), spd, device_id))

        conn.commit()
        conn.close()
        sess.db_trip_id = db_id
        print("[SYNC] Stored {} points for trip {}".format(len(points), trip_id))


# =========================================================================
# Self-tests (no LoRa, no hardware)
# =========================================================================
if __name__ == "__main__":
    import tempfile, shutil, os

    print("sync_manager.py self-test")
    print("-" * 50)
    failures = 0

    # Build a minimal fake DB
    work = tempfile.mkdtemp(prefix="syncmgr_")
    db_path = os.path.join(work, "test.db")
    profiles_path = os.path.join(work, "profiles.json")

    conn = sqlite3.connect(db_path)
    c = conn.cursor()
    c.execute("""CREATE TABLE trips (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        start_time TEXT, end_time TEXT,
        start_lat REAL, start_lon REAL, end_lat REAL, end_lon REAL,
        distance_km REAL, duration_seconds INTEGER,
        movement_type TEXT, profile_id TEXT,
        received_time TEXT, sync_status TEXT,
        manual_classification INTEGER DEFAULT 0,
        avg_speed_kmh REAL, max_speed_kmh REAL, device_id TEXT
    )""")
    c.execute("""CREATE TABLE trip_points (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        trip_id INTEGER, latitude REAL, longitude REAL,
        timestamp TEXT, distance_km REAL, speed_kmh REAL, device_id TEXT
    )""")
    conn.commit(); conn.close()

    with open(profiles_path, "w") as f:
        json.dump({"profiles": [
            {"id": "walking_commute", "type": "walking", "auto_assign": True}
        ]}, f)

    # Fake DB wrapper
    class FakeDB:
        def __init__(self): self.db_path = db_path

    # Capture sent messages
    sent = []
    def fake_send(msg):
        sent.append(msg)
        print("  SENT:", msg[:80])

    mgr = SyncManager(FakeDB(), fake_send, profiles_path)

    # Build test fixes
    test_fixes = [
        [1777674008, 50.127184, 14.12072,  421.6, 3.71],
        [1777674023, 50.127140, 14.120649, 412.1, 1.34],
        [1777674038, 50.127030, 14.120769, 402.5, 4.11],
        [1777674053, 50.126892, 14.121033, 416.8, 3.34],
        [1777674068, 50.126824, 14.121293, 433.4, 4.38],
    ]
    test_meta = {
        "id": "T1777674008", "device": "B1",
        "start_ts": 1777674008, "end_ts": 1777675203,
        "start_lat": 50.127184, "start_lon": 14.12072,
        "end_lat": 50.126824, "end_lon": 14.121293,
        "type": "walking", "km": 0.15, "dur": 600,
        "avg": 0.9, "max": 4.38,
    }

    # --- 1. SYNC -> QTRIPS ---
    print("\n[1] SYNC triggers QTRIPS:")
    sent.clear()
    mgr.on_message("SYNC:B1")
    ok = (len(sent) == 1 and sent[0] == "QTRIPS:B1")
    print("    {}".format("OK" if ok else "FAIL: " + str(sent)))
    if not ok: failures += 1

    # --- 2. RTRIPS -> QTRIP ---
    print("\n[2] RTRIPS triggers QTRIP for first trip:")
    sent.clear()
    mgr.on_message("RTRIPS:B1:T1777674008:5")
    ok = (len(sent) == 1 and sent[0] == "QTRIP:T1777674008")
    print("    {}".format("OK" if ok else "FAIL: " + str(sent)))
    if not ok: failures += 1

    # --- 3. RTRIP -> QPTS:0 ---
    print("\n[3] RTRIP triggers QPTS starting at 0:")
    sent.clear()
    meta_str = json.dumps(test_meta, separators=(",", ":"))
    mgr.on_message("RTRIP:T1777674008:" + meta_str)
    ok = (len(sent) == 1 and sent[0].startswith("QPTS:T1777674008:0:"))
    print("    {}".format("OK" if ok else "FAIL: " + str(sent)))
    if not ok: failures += 1

    # --- 4. RPTS with all fixes -> ACK + stored ---
    print("\n[4] RPTS with all fixes -> ACK + stored in DB:")
    sent.clear()
    from sync_codec import encode_rpts
    encoded, n = encode_rpts(test_fixes)
    mgr.on_message("RPTS:T1777674008:0:" + encoded)
    ok = (any(s == "ACK:T1777674008" for s in sent))
    print("    ACK sent: {}".format("OK" if ok else "FAIL: " + str(sent)))
    if not ok: failures += 1

    # Verify DB
    conn = sqlite3.connect(db_path)
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM trips")
    n_trips = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM trip_points")
    n_pts = c.fetchone()[0]
    conn.close()
    ok = (n_trips == 1 and n_pts == 5)
    print("    DB: {} trip(s), {} points  {}".format(
        n_trips, n_pts, "OK" if ok else "FAIL"))
    if not ok: failures += 1

    # --- 5. Re-sync same trip -> replaces points ---
    print("\n[5] Re-sync same trip replaces trip_points:")
    sent.clear()
    mgr.on_message("SYNC:B1")
    mgr.on_message("RTRIPS:B1:T1777674008:5")
    mgr.on_message("RTRIP:T1777674008:" + meta_str)
    extra_fix = [[1777674083, 50.12685, 14.121552, 435.3, 3.50]]
    all_fixes = test_fixes + extra_fix
    encoded2, _ = encode_rpts(all_fixes)
    mgr.on_message("RPTS:T1777674008:0:" + encoded2)
    conn = sqlite3.connect(db_path)
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM trips")
    n_trips2 = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM trip_points")
    n_pts2 = c.fetchone()[0]
    conn.close()
    ok = (n_trips2 == 1 and n_pts2 == 6)  # no dup trip, 6 points now
    print("    DB: {} trip(s), {} points (expect 1, 6)  {}".format(
        n_trips2, n_pts2, "OK" if ok else "FAIL"))
    if not ok: failures += 1

    shutil.rmtree(work)
    print()
    print("ALL SELF-TESTS PASSED" if failures == 0
          else "{} FAILURES".format(failures))