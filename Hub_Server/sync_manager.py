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
        # Per-message retry state
        self.last_sent_at  = 0.0    # time.time() when last Q* was sent
        self.last_sent_msg = None   # the last message we sent (for retry)
        self.msg_retries   = 0      # retry count for current message

    def reset(self):
        self.pending_trips = []
        self.current_trip  = None
        self.expected_from = 0
        self.total_pts     = 0
        self.meta          = None
        self.points        = []
        self.db_trip_id    = None
        self.last_sent_at  = 0.0
        self.last_sent_msg = None
        self.msg_retries   = 0


class SyncManager:
    """Handles the Pi 5 side of the sync protocol.

    db            -- TripDatabase instance (from receiver_pi5_advanced)
    send_fn       -- callable(text) that encrypts and sends over LoRa
    profiles_path -- path to profiles.json for auto-assign
    """
    QTRIPS_TIMEOUT_S  = 30   # resend QTRIPS if no RTRIPS within this time
    QTRIPS_MAX_RETRY  = 3    # give up after this many retries
    QMSG_TIMEOUT_S    = 15   # resend QTRIP/QPTS if no response within this time
    QMSG_MAX_RETRY    = 4    # give up on this trip after this many retries

    def __init__(self, db, send_fn, profiles_path=None):
        self.db            = db
        self.send          = send_fn
        self.profiles_path = profiles_path or os.path.expanduser(
            "~/trip_data/profiles.json")
        self._sessions     = {}   # hwid -> SyncSession (phase 3+)
        self._profiles     = None
        # Retry tracking: {hwid: {"sent_at": time, "retries": int}}
        self._qtrips_pending = {}

    def _resolve_to_hwid(self, value):
        """Accept either a hwid (the new wire prefix) or a renameable
        name (legacy / un-flashed device), return the hwid. None if
        the value isn't registered in `devices`."""
        try:
            conn = sqlite3.connect(self.db.db_path)
            c    = conn.cursor()
            c.execute("SELECT id FROM devices WHERE id=?", (value,))
            if c.fetchone():
                conn.close()
                return value
            c.execute("SELECT id FROM devices WHERE name=?", (value,))
            row = c.fetchone()
            conn.close()
            return row[0] if row else None
        except Exception as e:
            print("[SYNC] _resolve_to_hwid({}): {}".format(value, e))
            return None

    def _hwid_to_name(self, hwid):
        """Look up the friendly name for an hwid. Falls back to hwid
        if the device isn't in the table — keeps log lines readable."""
        try:
            conn = sqlite3.connect(self.db.db_path)
            c    = conn.cursor()
            c.execute("SELECT name FROM devices WHERE id=?", (hwid,))
            row = c.fetchone()
            conn.close()
            return row[0] if row else hwid
        except Exception:
            return hwid

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

    def _send_tracked(self, msg, device_id):
        """Send a Q* message and track it for timeout/retry in tick()."""
        import time as _time
        self.send(msg)
        sess = self._session(device_id)
        sess.last_sent_at  = _time.time()
        sess.last_sent_msg = msg
        sess.msg_retries   = 0

    def tick(self):
        """Call periodically (e.g. every 5s from hub's main loop).

        Two retry mechanisms:
        1. QTRIPS retry: if we sent QTRIPS but got no RTRIPS within
           QTRIPS_TIMEOUT_S, resend. Gives up after QTRIPS_MAX_RETRY.
        2. Per-message retry: if we sent QTRIP or QPTS but got no response
           within QMSG_TIMEOUT_S, resend the same message. If QMSG_MAX_RETRY
           exceeded, skip the problematic trip and move to the next one.
        """
        import time as _time
        now = _time.time()

        # --- QTRIPS retry ---
        for device_id, state in list(self._qtrips_pending.items()):
            elapsed = now - state["sent_at"]
            if elapsed >= self.QTRIPS_TIMEOUT_S:
                sess = self._session(device_id)
                if sess.pending_trips or sess.current_trip:
                    del self._qtrips_pending[device_id]
                    continue
                if state["retries"] >= self.QTRIPS_MAX_RETRY:
                    print("[SYNC] QTRIPS gave up after {} retries for {}".format(
                        self.QTRIPS_MAX_RETRY, device_id))
                    del self._qtrips_pending[device_id]
                    continue
                state["retries"] += 1
                state["sent_at"] = now
                msg = "QTRIPS:{}".format(device_id)
                self.send(msg)
                print("[SYNC] QTRIPS retry #{} for {}".format(
                    state["retries"], device_id))

        # --- Per-message (QTRIP/QPTS) retry ---
        for device_id, sess in self._sessions.items():
            if not sess.last_sent_msg or not sess.current_trip:
                continue
            elapsed = now - sess.last_sent_at
            if elapsed < self.QMSG_TIMEOUT_S:
                continue
            if sess.msg_retries >= self.QMSG_MAX_RETRY:
                print("[SYNC] Skipping trip {} after {} retries, moving on".format(
                    sess.current_trip, self.QMSG_MAX_RETRY))
                # Skip this trip, try next
                if sess.pending_trips:
                    sess.pending_trips.pop(0)
                sess.current_trip  = None
                sess.last_sent_msg = None
                sess.msg_retries   = 0
                self._request_next_trip(device_id)
                continue
            sess.msg_retries += 1
            sess.last_sent_at = now
            print("[SYNC] Retry #{} for {}: {}".format(
                sess.msg_retries, device_id, sess.last_sent_msg[:60]))
            self.send(sess.last_sent_msg)

    def _on_sync(self, value):
        """Device announces it has unsent data. `value` is either the
        hwid (new wire prefix from phase 3+ firmware) or the renameable
        name (legacy firmware). We always key the session by hwid.

        If a sync is already mid-flight for this device, do NOT reset
        the session — the device's periodic SYNC re-announce (every
        ~5 min) would otherwise blow away accumulated RPTS fixes and
        force a restart from batch 0. The per-message retry logic in
        tick() handles genuinely stuck sessions; ignoring re-announces
        while syncing is safe."""
        hwid = self._resolve_to_hwid(value)
        if not hwid:
            print("[SYNC] SYNC from unknown device: {}".format(value))
            return True
        name = self._hwid_to_name(hwid)
        sess = self._session(hwid)
        if sess.current_trip or sess.pending_trips:
            print("[SYNC] re-announce from {} while sync active "
                  "(current={}, pending={}); continuing".format(
                      name, sess.current_trip,
                      len(sess.pending_trips)))
            return True
        print("[SYNC] SYNC from {} ({})".format(name, hwid[:8]))
        sess.reset()
        # Reply with hwid; firmware accepts both formats but we prefer
        # the rename-proof one.
        msg = "QTRIPS:{}".format(hwid)
        self.send(msg)
        print("[SYNC] -> {}".format(msg))
        import time as _time
        self._qtrips_pending[hwid] = {
            "sent_at": _time.time(), "retries": 0}
        return True

    def _load_deleted(self):
        """Return set of (start_time, device_id) tuples for deleted trips."""
        deleted_path = os.path.join(
            os.path.dirname(self.db.db_path), 'deleted_trips.json')
        try:
            if os.path.exists(deleted_path):
                with open(deleted_path) as f:
                    records = json.load(f)
                return {(r.get('start_time'), r.get('device_id'))
                        for r in records}
        except Exception:
            pass
        return set()

    def _is_deleted(self, trip_id, hwid):
        """Check if a device-side trip_id was previously deleted from
        Pi 5's DB. Match by start_ts embedded in the trip_id and the
        device — checking both the hwid (modern) and the current name
        (deleted_trips.json was populated with names historically)."""
        deleted = self._load_deleted()
        if not deleted:
            return False
        try:
            ts = int(trip_id[1:])
            start_iso = _to_iso(ts)
            name = self._hwid_to_name(hwid)
            return ((start_iso, hwid) in deleted or
                    (start_iso, name) in deleted)
        except Exception:
            return False

    def _on_rtrips(self, payload):
        """RTRIPS:<hwid_or_name>:T123:72,T456:45"""
        parts = payload.split(":", 1)
        if len(parts) != 2:
            return True
        value, trips_str = parts
        hwid = self._resolve_to_hwid(value)
        if not hwid:
            print("[SYNC] RTRIPS from unknown device: {}".format(value))
            return True
        device_id = hwid  # session key is now hwid; local var name kept
        sess = self._session(hwid)
        # If a sync is already in progress, don't restart it. RTRIPS
        # from a redundant SYNC re-announce would otherwise wipe
        # accumulated RPTS fixes.
        if sess.current_trip or sess.pending_trips:
            print("[SYNC] RTRIPS while sync active (current={}, "
                  "pending={}); ignoring".format(
                      sess.current_trip, len(sess.pending_trips)))
            self._qtrips_pending.pop(hwid, None)
            return True
        sess.reset()
        # Clear retry tracking — we got a response
        self._qtrips_pending.pop(hwid, None)

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

        # Filter out trips previously deleted from the DB.
        # Send ACK immediately so Pico B marks them confirmed and stops
        # re-announcing them.
        filtered = []
        for tid, npts in trip_pairs:
            if self._is_deleted(tid, device_id):
                print("[SYNC] {} was deleted from DB - sending ACK to suppress".format(tid))
                self.send("ACK:{}".format(tid))
            else:
                filtered.append(tid)

        sess.pending_trips = filtered
        sess._npts_map = {t: n for t, n in trip_pairs}

        if not filtered:
            print("[SYNC] All trips already handled for {}".format(device_id))
            return True

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
        self._send_tracked(msg, device_id)
        print("[SYNC] -> {} (expecting {} pts)".format(msg, sess.total_pts))

    def _normalize_meta(self, meta):
        """Accept both compact (short key) and verbose (long key) meta dicts.
        Pico B sends compact keys to fit in LoRa packets.  Returns a
        normalized dict always using long keys.

        Carries through 'hwid' if the device emitted one (Phase 2b+).
        Older firmware doesn't include it; callers fall back to a
        devices.name lookup in that case.
        """
        def _get(long_k, short_k, default=None):
            return meta.get(long_k) if meta.get(long_k) is not None \
                   else meta.get(short_k, default)
        return {
            "device":    meta.get("device") or meta.get("d"),
            "hwid":      meta.get("hwid"),
            "type":      meta.get("type", "unknown"),
            "start_ts":  _get("start_ts",  "sts"),
            "end_ts":    _get("end_ts",    "ets"),
            "start_lat": _get("start_lat", "slat"),
            "start_lon": _get("start_lon", "slon"),
            "end_lat":   _get("end_lat",   "elat"),
            "end_lon":   _get("end_lon",   "elon"),
            "km":        _get("km",        "km"),
            "dur":       _get("dur",       "dur"),
            "avg":       _get("avg",       "avg"),
            "max":       _get("max",       "max"),
        }

    def _resolve_hwid(self, device_id, meta=None):
        """Look up the hwid for a session/device. Prefer the meta's
        own hwid (Phase 2b+ firmware); fall back to a devices.name
        lookup. Returns None if we can't resolve — caller writes NULL
        to the device_hwid column and the Phase 1 backfill will fix
        it on next Pi startup."""
        if meta and meta.get("hwid"):
            return meta["hwid"]
        try:
            conn = sqlite3.connect(self.db.db_path)
            c    = conn.cursor()
            c.execute("SELECT id FROM devices WHERE name=?", (device_id,))
            row = c.fetchone()
            conn.close()
            return row[0] if row else None
        except Exception as e:
            print("[SYNC] _resolve_hwid({}): {}".format(device_id, e))
            return None

    def _on_rtrip(self, payload):
        """RTRIP:T123:{json_meta}"""
        colon = payload.find(":")
        if colon < 0:
            return True
        trip_id  = payload[:colon]
        meta_str = payload[colon+1:]
        try:
            raw_meta = json.loads(meta_str)
        except Exception as e:
            print("[SYNC] RTRIP parse error:", e)
            return True

        meta = self._normalize_meta(raw_meta)

        # Look up the session by trip_id (same approach as _on_rpts).
        # Session keys are hwids (phase 3). Prefer meta.hwid if present;
        # otherwise scan for the trip across all sessions — trip_id is
        # globally unique enough to identify the owner.
        hwid = meta.get("hwid")
        if hwid and hwid in self._sessions:
            session_key = hwid
        else:
            session_key = None
            for dev, s in self._sessions.items():
                if s.current_trip == trip_id:
                    session_key = dev
                    break
        if session_key is None:
            print("[SYNC] RTRIP: unexpected trip_id {}".format(trip_id))
            return True
        sess = self._session(session_key)
        # Stash the current friendly name in the meta so _store_trip
        # writes the trip under the right (canonical) label, not the
        # stale name the device may still carry in its local JSON.
        meta["device"] = self._hwid_to_name(session_key)
        meta["hwid"]   = session_key   # always carry forward
        sess.meta = meta
        device_id = session_key  # downstream uses hwid as session key
        print("[SYNC] Got meta for {} type={} km={} pts={}".format(
            trip_id, meta.get("type"), meta.get("km"), sess.total_pts))

        # Request first batch of fixes — clear retry counter since RTRIP received
        sess.msg_retries = 0
        count = min(BATCH_SIZE, max(sess.total_pts, BATCH_SIZE))
        msg = "QPTS:{}:{}:{}".format(trip_id, 0, count)
        self._send_tracked(msg, device_id)
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

        # Detect a transient empty batch — PicoB returned no fixes but
        # the RTRIP meta said there should be more (e.g. trip declared
        # 86 points, we've stored 0). Treating that as 'done' was the
        # bug behind today's car trips appearing with empty polylines:
        # one bad RPTS from a marginal-RSSI moment ACKed the whole trip
        # at zero. Don't reset retries; tick() resends QPTS up to
        # QMSG_MAX_RETRY, then properly skips the trip.
        expected_more = (sess.total_pts > 0 and
                         len(sess.points) < sess.total_pts)
        if not fixes and expected_more:
            print("[SYNC] empty RPTS from idx {} for {} ({}/{} pts) — "
                  "awaiting retry".format(
                      from_idx, trip_id,
                      len(sess.points), sess.total_pts))
            return True

        # Good response received — reset retry counter
        sess.msg_retries = 0

        # Are we done? Hub knows we've got everything when either the
        # device told us via meta (total_pts) and we've reached it, OR
        # we asked and PicoB had nothing more to send (empty batch
        # arriving with no expected_more).
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
                self._send_tracked(msg, device_id)
                print("[SYNC] -> {}".format(msg))

        if done:
            self._store_trip(device_id, trip_id)
            # Send ACK - Pico B marks as confirmed on receive
            msg = "ACK:{}".format(trip_id)
            self.send(msg)
            print("[SYNC] -> {} ({} pts stored)".format(msg, len(sess.points)))
            # Move to next trip. Tolerate empty pending_trips — happens when
            # a duplicate RPTS arrives after the trip has already been ACKed
            # and popped (PicoB retransmits, hub already moved on). Without
            # this guard the reader thread crashed and all incoming traffic
            # froze until a restart.
            if sess.pending_trips:
                sess.pending_trips.pop(0)
            self._request_next_trip(device_id)

        return True

    def _store_trip(self, device_id, trip_id):
        """Write a completed sync'd trip to the Pi 5 database.

        Note: from phase 3 onwards `device_id` arrives here as the hwid
        (session key). The DB column trips.device_id stores the friendly
        name, so we resolve it; trips.device_hwid stores the hwid.
        """
        sess = self._session(device_id)
        meta   = sess.meta or {}
        points = sess.points

        movement_type = meta.get("type") or "unknown"
        profiles      = self._get_profiles()
        profile_id    = profiles.get(movement_type)
        # `device_id` is hwid; resolve back to the current friendly name
        # for the DB row. (Phase 4 will drop name-keying entirely.)
        hwid          = device_id
        name          = self._hwid_to_name(hwid)
        device_id     = name   # local rename for the rest of this method

        conn = sqlite3.connect(self.db.db_path)
        c    = conn.cursor()

        # Find existing trip row if any. Prefer the hwid match (stable
        # across renames); fall back to name + start_time for legacy
        # rows that pre-date the device_hwid column.
        start_iso = _to_iso(meta.get("start_ts"))
        existing_id = None
        if start_iso:
            c.execute(
                "SELECT id FROM trips WHERE device_hwid=? AND start_time=? LIMIT 1",
                (hwid, start_iso))
            row = c.fetchone()
            if not row and name:
                c.execute(
                    "SELECT id FROM trips WHERE device_id=? AND start_time=? LIMIT 1",
                    (name, start_iso))
                row = c.fetchone()
            if row:
                existing_id = row[0]

        if existing_id:
            # Update existing trip metadata (Pico B is authoritative)
            c.execute('''UPDATE trips SET
                end_time=?, start_lat=?, start_lon=?, end_lat=?, end_lon=?,
                distance_km=?, duration_seconds=?, movement_type=?,
                avg_speed_kmh=?, max_speed_kmh=?, sync_status=?,
                device_hwid=COALESCE(?, device_hwid)
                WHERE id=?''',
                (_to_iso(meta.get("end_ts")),
                 meta.get("start_lat"), meta.get("start_lon"),
                 meta.get("end_lat"),   meta.get("end_lon"),
                 meta.get("km"),        meta.get("dur"),
                 movement_type,
                 meta.get("avg"),       meta.get("max"),
                 "synced", hwid, existing_id))
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
                 avg_speed_kmh, max_speed_kmh, device_id, device_hwid)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                (_to_iso(meta.get("start_ts")),
                 _to_iso(meta.get("end_ts")),
                 meta.get("start_lat"), meta.get("start_lon"),
                 meta.get("end_lat"),   meta.get("end_lon"),
                 meta.get("km"),        meta.get("dur"),
                 movement_type, profile_id,
                 datetime.now().isoformat(), "synced", 0,
                 meta.get("avg"),       meta.get("max"),
                 device_id, hwid))
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
                (trip_id, latitude, longitude, timestamp,
                 distance_km, speed_kmh, device_id, device_hwid)
                VALUES (?,?,?,?,?,?,?,?)''',
                (db_id, lat, lon, _to_iso(ts),
                 round(cumulative_km, 4), spd, device_id, hwid))

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