#!/usr/bin/env python3
"""
Enhanced Trip Data Receiver with Advanced Statistics
- Speed distribution histogram
- Fastest/slowest trip tracking
- Speed trend over time
- Multiple profiles per movement type
"""

import json
import time
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from flask import Flask, jsonify, request, send_from_directory
import threading
import queue

# ============================================================================
# CONFIGURATION
# ============================================================================

import os as _os

# Data lives next to this script by default — the workspace is now
# self-contained per-project, with trips.db and profiles.json inside
# Trip_Hub/. Override with TRIP_HUB_DATA_DIR for legacy / multi-host setups.
SCRIPT_DIR    = Path(__file__).resolve().parent
DATA_DIR      = Path(_os.environ.get('TRIP_HUB_DATA_DIR', str(SCRIPT_DIR)))
DATABASE_FILE = DATA_DIR / 'trips.db'
LOG_FILE      = DATA_DIR / 'receiver.log'
PROFILES_FILE = DATA_DIR / 'profiles.json'

# Hub_Server's serial-bridge log. Override with HUB_SERVER_LOG env var if
# Trip_Hub runs from a different cwd or in a different environment.
HUB_SERVER_LOG = Path(_os.environ.get(
    'HUB_SERVER_LOG',
    str(SCRIPT_DIR.parent / 'Hub_Server' / 'picoA_serial.log')))

DATA_DIR.mkdir(parents=True, exist_ok=True)

# Default profiles
DEFAULT_PROFILES = {
    "profiles": [
        {
            "id": "walking",
            "name": "Walking",
            "type": "walking",
            "icon": "🚶",
            "color": "#4CAF50",
            "auto_assign": True
        },
        {
            "id": "cycling",
            "name": "Cycling",
            "type": "cycling",
            "icon": "🚴",
            "color": "#2196F3",
            "auto_assign": True
        },
        {
            "id": "driving",
            "name": "Driving",
            "type": "driving",
            "icon": "🚗",
            "color": "#f44336",
            "auto_assign": True
        }
    ]
}

# ============================================================================
# PROFILE MANAGER
# ============================================================================

class ProfileManager:
    """Manages activity profiles"""
    
    def __init__(self, profiles_file=PROFILES_FILE):
        self.profiles_file = profiles_file
        self.profiles = {}
        self.load_profiles()
    
    def load_profiles(self):
        """Load profiles from file, use defaults if not exist"""
        try:
            with open(self.profiles_file, 'r') as f:
                data = json.load(f)
                self.profiles = {p['id']: p for p in data.get('profiles', [])}
            print(f"[Profiles] Loaded {len(self.profiles)} profiles")
        except FileNotFoundError:
            print("[Profiles] No profiles file, using defaults")
            self.profiles = {p['id']: p for p in DEFAULT_PROFILES['profiles']}
            self.save_profiles()
    
    def save_profiles(self):
        """Save profiles to file"""
        try:
            data = {'profiles': list(self.profiles.values())}
            with open(self.profiles_file, 'w') as f:
                json.dump(data, f, indent=2)
            print("[Profiles] Saved")
        except Exception as e:
            print(f"[Profiles] Save error: {e}")
    
    def get_all_profiles(self):
        """Get all profiles"""
        return list(self.profiles.values())
    
    def get_profiles_by_type(self, movement_type):
        """Get profiles for a movement type"""
        return [p for p in self.profiles.values() if p['type'] == movement_type]
    
    def get_profile(self, profile_id):
        """Get a specific profile"""
        return self.profiles.get(profile_id)

    def get_default_profile_for_type(self, movement_type):
        """Return the auto_assign:true profile for a given movement_type,
        falling back to the first profile of that type if none are flagged.
        Returns None if no profile of that type exists."""
        candidates = self.get_profiles_by_type(movement_type)
        if not candidates:
            return None
        for p in candidates:
            if p.get('auto_assign'):
                return p
        return candidates[0]
    
    def add_profile(self, profile_id, name, movement_type, icon, color):
        """Add a new profile"""
        profile = {
            'id': profile_id,
            'name': name,
            'type': movement_type,
            'icon': icon,
            'color': color,
            'auto_assign': False,
            'created': datetime.now().isoformat()
        }
        self.profiles[profile_id] = profile
        self.save_profiles()
        return profile
    
    def update_profile(self, profile_id, **kwargs):
        """Update a profile"""
        if profile_id in self.profiles:
            self.profiles[profile_id].update(kwargs)
            self.save_profiles()
            return self.profiles[profile_id]
        return None
    
    def delete_profile(self, profile_id):
        """Delete a profile"""
        if profile_id in self.profiles:
            del self.profiles[profile_id]
            self.save_profiles()
            return True
        return False

# ============================================================================
# DATABASE - ENHANCED WITH ADVANCED STATS
# ============================================================================

def _filter_clauses(days=7, device_id=None, from_iso=None, to_iso=None,
                    table_alias=''):
    """Build (where_sql, params) for the standard time-window + device-id
    filter shared by /api/stats, /api/recent and the per-profile aggregates.

    table_alias  '' for plain `SELECT ... FROM trips` queries; pass 't' when
                 trips are joined with another table and need the prefix.

    Precedence: explicit `from`/`to` always wins over the rolling `days`
    window. Either-but-not-both is also valid.
    """
    pre = (table_alias + '.') if table_alias else ''
    where = []
    params = []
    if from_iso and to_iso:
        where.append(f"{pre}start_time >= ?"); params.append(from_iso)
        where.append(f"{pre}start_time <  ?"); params.append(to_iso)
    elif from_iso:
        where.append(f"{pre}start_time >= ?"); params.append(from_iso)
    elif to_iso:
        where.append(f"{pre}start_time <  ?"); params.append(to_iso)
    else:
        where.append(f"{pre}start_time >= datetime('now', '-' || ? || ' days')")
        params.append(days)
    if device_id:
        where.append(f"{pre}device_id = ?")
        params.append(device_id)
    return ' AND '.join(where), params


class TripDatabase:
    """SQLite database with advanced statistics"""
    
    def __init__(self, db_path=DATABASE_FILE, profiles_file=PROFILES_FILE):
        self.db_path = db_path
        self.profiles_file = profiles_file
        self.init_db()
    
    def init_db(self):
        """Initialize database schema"""
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        
        c.execute('''CREATE TABLE IF NOT EXISTS trips (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            start_time TEXT,
            end_time TEXT,
            start_lat REAL,
            start_lon REAL,
            end_lat REAL,
            end_lon REAL,
            distance_km REAL,
            duration_seconds INTEGER,
            movement_type TEXT,
            profile_id TEXT,
            received_time TEXT,
            sync_status TEXT,
            manual_classification INTEGER DEFAULT 0,
            avg_speed_kmh REAL,
            max_speed_kmh REAL
        )''')
        
        c.execute('''CREATE TABLE IF NOT EXISTS trip_points (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            trip_id INTEGER,
            latitude REAL,
            longitude REAL,
            timestamp TEXT,
            distance_km REAL,
            speed_kmh REAL,
            FOREIGN KEY(trip_id) REFERENCES trips(id)
        )''')
        
        c.execute('''CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_type TEXT,
            trip_id INTEGER,
            latitude REAL,
            longitude REAL,
            timestamp TEXT,
            movement_type TEXT,
            profile_id TEXT,
            received_time TEXT,
            FOREIGN KEY(trip_id) REFERENCES trips(id)
        )''')
        
        c.execute('''CREATE TABLE IF NOT EXISTS profile_changes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            trip_id INTEGER,
            old_profile_id TEXT,
            new_profile_id TEXT,
            changed_at TEXT,
            reason TEXT,
            FOREIGN KEY(trip_id) REFERENCES trips(id)
        )''')

        # ---- Live GPS points (real-time stream from LoRa devices) ----
        # NOT tied to trips. Just a rolling buffer of recent points so the
        # map can show current position. Auto-pruned to last 7 days.
        c.execute('''CREATE TABLE IF NOT EXISTS live_points (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            recv_at TEXT NOT NULL,
            lat REAL NOT NULL,
            lon REAL NOT NULL,
            alt REAL,
            spd REAL,
            ts INTEGER,
            rssi INTEGER,
            snr REAL,
            source TEXT
        )''')
        c.execute('''CREATE INDEX IF NOT EXISTS idx_live_points_recv_at
                     ON live_points(recv_at)''')

        # ---- Pending LoRa ops (Trip_Hub → Hub_Server cross-process queue) ----
        # Trip_Hub inserts rows; Hub_Server's worker picks them up, sends
        # the LoRa packet, and marks status. Used today for device-rename;
        # design generalises to any one-shot LoRa request/reply pair.
        c.execute('''CREATE TABLE IF NOT EXISTS pending_lora_ops (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            op TEXT NOT NULL,
            payload TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            enqueued_at TEXT NOT NULL,
            started_at TEXT,
            completed_at TEXT,
            error TEXT
        )''')

        # Maps a device's permanent hardware id to its current renameable
        # label. The hwid is hex-encoded RP2040 unique_id() / ESP32 chip
        # MAC — burned at the factory, never changes. Hub_Server upserts
        # this on every DEVICE: announce; rename = name change for an
        # already-known id, which is when we cascade the new name into
        # trips / live_points / messages.
        c.execute('''CREATE TABLE IF NOT EXISTS devices (
            id        TEXT PRIMARY KEY,
            name      TEXT NOT NULL,
            last_seen TEXT,
            last_rssi INTEGER
        )''')

        # ---- Chat messages (LoRa text traffic) ----
        # Both directions: 'rx' = received from a remote LoRa device,
        # 'tx' = sent out from this Pi 5 (typed in console, or via web).
        # Status only matters for tx: pending -> sent | failed.
        # Auto-pruned to last 7 days.
        c.execute('''CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            direction TEXT NOT NULL,
            text TEXT NOT NULL,
            recv_at TEXT NOT NULL,
            rssi INTEGER,
            snr REAL,
            source TEXT,
            status TEXT DEFAULT 'sent',
            sent_at TEXT,
            error TEXT
        )''')
        c.execute('''CREATE INDEX IF NOT EXISTS idx_messages_id
                     ON messages(id)''')
        c.execute('''CREATE INDEX IF NOT EXISTS idx_messages_status
                     ON messages(status)''')

        c.execute('''CREATE TABLE IF NOT EXISTS journeys (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            created_at TEXT NOT NULL
        )''')

        c.execute('''CREATE TABLE IF NOT EXISTS journey_trips (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            journey_id INTEGER NOT NULL REFERENCES journeys(id) ON DELETE CASCADE,
            trip_id INTEGER NOT NULL REFERENCES trips(id) ON DELETE CASCADE,
            position INTEGER NOT NULL DEFAULT 0
        )''')
        c.execute('''CREATE UNIQUE INDEX IF NOT EXISTS idx_jt_unique
                     ON journey_trips(journey_id, trip_id)''')

        # ---- Migrations: add device_id columns if missing ----
        for table in ('trips', 'trip_points', 'events'):
            try:
                c.execute(f"ALTER TABLE {table} ADD COLUMN device_id TEXT")
                print(f"[DB] Added device_id column to {table}")
            except sqlite3.OperationalError:
                pass

        # ---- Migration: last_rssi on devices (signal-strength of last announce) ----
        try:
            c.execute("ALTER TABLE devices ADD COLUMN last_rssi INTEGER")
            print("[DB] Added last_rssi column to devices")
        except sqlite3.OperationalError:
            pass

        # ---- Migration: add device_hwid columns + backfill ----
        # The permanent hwid (RP2040 unique_id / ESP32 chip MAC) is the
        # rename-proof key. We carry it alongside the renameable name so
        # any future rename never disturbs existing rows. NULL rows from
        # before this migration get backfilled by joining the current
        # name through `devices` to its hwid.
        for table, name_col in (('trips',       'device_id'),
                                ('trip_points', 'device_id'),
                                ('live_points', 'source'),
                                ('messages',    'source')):
            try:
                c.execute(f"ALTER TABLE {table} ADD COLUMN device_hwid TEXT")
                print(f"[DB] Added device_hwid column to {table}")
            except sqlite3.OperationalError:
                pass
            c.execute(f"""UPDATE {table}
                          SET device_hwid = (
                              SELECT id FROM devices
                              WHERE devices.name = {table}.{name_col})
                          WHERE device_hwid IS NULL
                            AND {name_col} IS NOT NULL""")
            if c.rowcount > 0:
                print(f"[DB] Backfilled device_hwid on {c.rowcount} "
                      f"{table} rows")

        conn.commit()
        conn.close()
    
    def add_trip(self, trip_data, profile_id=None, device_id=None,
                 device_hwid=None):
        """Add a completed trip to database.

        device_hwid is the permanent hwid from Phase 2b+ firmware. If
        absent, resolve via the devices table from device_id. Older
        callers that don't pass it still work.
        """
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()

        if not device_hwid and device_id:
            c.execute("SELECT id FROM devices WHERE name=?", (device_id,))
            row = c.fetchone()
            if row:
                device_hwid = row[0]
        elif device_hwid and not device_id:
            # Hwid provided but no name — resolve via devices.
            c.execute("SELECT name FROM devices WHERE id=?", (device_hwid,))
            row = c.fetchone()
            if row:
                device_id = row[0]

        movement_type = trip_data.get('movement_type', 'unknown')
        
        # Calculate speeds for the trip
        points = trip_data.get('points', [])
        max_speed = max([p.get('speed_kmh', 0) for p in points]) if points else 0
        
        if trip_data.get('duration_seconds', 0) > 0:
            avg_speed = trip_data.get('distance_km', 0) / (trip_data.get('duration_seconds', 1) / 3600)
        else:
            avg_speed = 0

        # If caller provided explicit avg / max speed (e.g. from Pico's TRIPEND
        # message), use those.  Otherwise stick with the computed values.
        if 'avg_speed_kmh' in trip_data and trip_data['avg_speed_kmh'] is not None:
            avg_speed = trip_data['avg_speed_kmh']
        if 'max_speed_kmh' in trip_data and trip_data['max_speed_kmh'] is not None:
            max_speed = trip_data['max_speed_kmh']

        c.execute('''INSERT INTO trips
            (start_time, end_time, start_lat, start_lon, end_lat, end_lon,
             distance_km, duration_seconds, movement_type, profile_id,
             received_time, sync_status, manual_classification, avg_speed_kmh,
             max_speed_kmh, device_id, device_hwid)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
            (
                trip_data.get('start_time'),
                trip_data.get('end_time'),
                trip_data.get('start_lat'),
                trip_data.get('start_lon'),
                trip_data.get('end_lat'),
                trip_data.get('end_lon'),
                trip_data.get('distance_km'),
                trip_data.get('duration_seconds'),
                movement_type,
                profile_id,
                datetime.now().isoformat(),
                'synced',
                0,
                avg_speed,
                max_speed,
                device_id,
                device_hwid,
            )
        )

        trip_id = c.lastrowid

        # Add points if provided
        for point in points:
            c.execute('''INSERT INTO trip_points
                (trip_id, latitude, longitude, timestamp,
                 distance_km, speed_kmh, device_id, device_hwid)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)''',
                (
                    trip_id,
                    point.get('lat'),
                    point.get('lon'),
                    point.get('timestamp'),
                    point.get('distance', 0),
                    point.get('speed_kmh', 0),
                    device_id,
                    device_hwid,
                )
            )
        
        conn.commit()
        conn.close()
        
        return trip_id
    
    def get_statistics_by_profile(self, days=7, device_id=None,
                                   from_iso=None, to_iso=None):
        """Get comprehensive statistics by profile, filtered by time window
        and optionally by device_id."""
        where, params = _filter_clauses(days, device_id, from_iso, to_iso)
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        c.execute(f'''SELECT profile_id,
            COUNT(*) as trip_count,
            SUM(distance_km) as total_distance,
            AVG(distance_km) as avg_distance,
            SUM(duration_seconds) as total_duration,
            AVG(avg_speed_kmh) as avg_speed,
            MAX(max_speed_kmh) as max_speed,
            MIN(max_speed_kmh) as min_speed
            FROM trips
            WHERE {where}
            GROUP BY profile_id
            ORDER BY total_distance DESC''', params)
        
        stats = {}
        for row in c.fetchall():
            profile_id = row[0]
            # Skip rows with NULL profile_id (legacy trips before
            # auto-assign existed). Flask's json encoder sorts keys and
            # can't compare None with strings on Python 3.11+. The
            # frontend only queries stats[<real_profile_id>] anyway.
            if profile_id is None:
                continue
            trip_count = row[1] or 0
            total_distance = row[2] or 0.0
            total_duration = (row[4] or 0) / 3600

            stats[profile_id] = {
                'trip_count': trip_count,
                'total_distance_km': total_distance,
                'avg_distance_km': row[3] or 0.0,
                'total_duration_hours': total_duration,
                'avg_speed_kmh': row[5] or 0.0,
                'max_speed_kmh': row[6] or 0.0,
                'min_speed_kmh': row[7] or 0.0
            }
        
        conn.close()
        return stats
    
    def get_speed_distribution(self, profile_id, days=7, device_id=None,
                                from_iso=None, to_iso=None):
        """Get speed distribution histogram data for profile."""
        where, params = _filter_clauses(days, device_id, from_iso, to_iso,
                                         table_alias='t')
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        c.execute(f'''SELECT tp.speed_kmh
            FROM trip_points tp
            JOIN trips t ON tp.trip_id = t.id
            WHERE t.profile_id = ? AND {where} AND tp.speed_kmh > 0
            ORDER BY tp.speed_kmh''', [profile_id] + params)
        
        speeds = [row[0] for row in c.fetchall()]
        
        if not speeds:
            conn.close()
            return {
                'bins': [],
                'counts': [],
                'min_speed': 0,
                'max_speed': 0,
                'median_speed': 0
            }
        
        # Create histogram with 10 bins
        min_speed = min(speeds)
        max_speed = max(speeds)
        
        if min_speed == max_speed:
            bin_edges = [min_speed, max_speed + 1]
        else:
            bin_width = (max_speed - min_speed) / 10
            bin_edges = [min_speed + (i * bin_width) for i in range(11)]
        
        # Count speeds in each bin
        histogram = {}
        for i in range(len(bin_edges) - 1):
            bin_label = f"{bin_edges[i]:.1f}-{bin_edges[i+1]:.1f}"
            count = sum(1 for s in speeds if bin_edges[i] <= s < bin_edges[i+1])
            histogram[bin_label] = count
        
        # Calculate median
        sorted_speeds = sorted(speeds)
        if len(sorted_speeds) % 2 == 0:
            median = (sorted_speeds[len(sorted_speeds)//2 - 1] + sorted_speeds[len(sorted_speeds)//2]) / 2
        else:
            median = sorted_speeds[len(sorted_speeds)//2]
        
        conn.close()
        
        return {
            'bins': list(histogram.keys()),
            'counts': list(histogram.values()),
            'min_speed': min_speed,
            'max_speed': max_speed,
            'median_speed': median,
            'total_points': len(speeds)
        }
    
    def get_fastest_slowest_trips(self, profile_id, days=7, device_id=None,
                                    from_iso=None, to_iso=None):
        """Get fastest and slowest trips in profile."""
        where, params = _filter_clauses(days, device_id, from_iso, to_iso)
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        # Fastest trip
        c.execute(f'''SELECT id, start_time, end_time, distance_km, duration_seconds, max_speed_kmh, avg_speed_kmh
            FROM trips
            WHERE profile_id = ? AND {where}
            ORDER BY max_speed_kmh DESC
            LIMIT 1''', [profile_id] + params)

        fastest = c.fetchone()
        fastest_trip = None
        if fastest:
            fastest_trip = {
                'id': fastest[0],
                'date': fastest[1],
                'distance_km': fastest[3],
                'duration_seconds': fastest[4],
                'max_speed_kmh': fastest[5],
                'avg_speed_kmh': fastest[6]
            }
        
        # Slowest trip (minimum average speed, excluding very short trips)
        c.execute(f'''SELECT id, start_time, end_time, distance_km, duration_seconds, max_speed_kmh, avg_speed_kmh
            FROM trips
            WHERE profile_id = ? AND {where} AND distance_km > 0.5
            ORDER BY avg_speed_kmh ASC
            LIMIT 1''', [profile_id] + params)
        
        slowest = c.fetchone()
        slowest_trip = None
        if slowest:
            slowest_trip = {
                'id': slowest[0],
                'date': slowest[1],
                'distance_km': slowest[3],
                'duration_seconds': slowest[4],
                'max_speed_kmh': slowest[5],
                'avg_speed_kmh': slowest[6]
            }
        
        conn.close()
        
        return {
            'fastest': fastest_trip,
            'slowest': slowest_trip
        }
    
    def get_speed_trend(self, profile_id, days=7, device_id=None,
                          from_iso=None, to_iso=None):
        """Get speed trend over time (daily average)."""
        where, params = _filter_clauses(days, device_id, from_iso, to_iso)
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        c.execute(f'''SELECT
            DATE(start_time) as trip_date,
            COUNT(*) as trip_count,
            AVG(avg_speed_kmh) as daily_avg_speed,
            MAX(max_speed_kmh) as daily_max_speed
            FROM trips
            WHERE profile_id = ? AND {where}
            GROUP BY DATE(start_time)
            ORDER BY trip_date ASC''', [profile_id] + params)
        
        trend = []
        for row in c.fetchall():
            trend.append({
                'date': row[0],
                'trip_count': row[1],
                'daily_avg_speed': row[2] or 0.0,
                'daily_max_speed': row[3] or 0.0
            })
        
        conn.close()
        return trend
    
    def change_trip_profile(self, trip_id, new_profile_id, reason="Manual reclassification"):
        """Move trip to a different profile"""
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        
        c.execute('SELECT profile_id FROM trips WHERE id = ?', (trip_id,))
        row = c.fetchone()
        old_profile_id = row[0] if row else None
        
        c.execute('UPDATE trips SET profile_id = ?, manual_classification = 1 WHERE id = ?',
                  (new_profile_id, trip_id))
        
        c.execute('''INSERT INTO profile_changes 
            (trip_id, old_profile_id, new_profile_id, changed_at, reason)
            VALUES (?, ?, ?, ?, ?)''',
            (trip_id, old_profile_id, new_profile_id, datetime.now().isoformat(), reason))
        
        conn.commit()
        conn.close()
        
        return True
    
    def get_trip_details(self, trip_id):
        """Get detailed trip information"""
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        
        c.execute('SELECT * FROM trips WHERE id = ?', (trip_id,))
        row = c.fetchone()
        
        if not row:
            conn.close()
            return None
        
        trip = {
            'id': row[0],
            'start_time': row[1],
            'end_time': row[2],
            'start_lat': row[3],
            'start_lon': row[4],
            'end_lat': row[5],
            'end_lon': row[6],
            'distance_km': row[7],
            'duration_seconds': row[8],
            'movement_type': row[9],
            'profile_id': row[10],
            'manual_classification': row[13],
            'avg_speed_kmh': row[14],
            'max_speed_kmh': row[15],
            'points': []
        }
        
        c.execute('SELECT latitude, longitude, timestamp, distance_km, speed_kmh FROM trip_points WHERE trip_id = ? ORDER BY id',
                  (trip_id,))
        for point_row in c.fetchall():
            trip['points'].append({
                'lat': point_row[0],
                'lon': point_row[1],
                'timestamp': point_row[2],
                'distance': point_row[3],
                'speed_kmh': point_row[4]
            })

        # Fallback: trip_points may be empty for trips synced from a device
        # whose local .gps file was lost (LittleFS reformat, etc.) — the
        # device's RPTS reply was an empty array, so the per-fix path is
        # gone. Reconstruct the polyline from live_points (the realtime
        # GPS broadcast firehose) filtered to the trip's device + time
        # window. Less precise than .gps samples but visually correct.
        if not trip['points']:
            device_id = row[16] if len(row) > 16 else None
            if device_id and trip['start_time'] and trip['end_time']:
                c.execute('''SELECT lat, lon, recv_at, spd
                             FROM live_points
                             WHERE source = ?
                               AND recv_at >= ?
                               AND recv_at <= ?
                             ORDER BY recv_at''',
                          (device_id, trip['start_time'], trip['end_time']))
                for lp in c.fetchall():
                    trip['points'].append({
                        'lat':       lp[0],
                        'lon':       lp[1],
                        'timestamp': lp[2],
                        'distance':  0,
                        'speed_kmh': lp[3] or 0,
                    })

        conn.close()
        return trip
    
    def list_devices(self):
        """Devices visible in the picker — union of:
          - distinct device_ids in `trips` (with trip count + last trip),
          - paired devices in `devices` that haven't logged a trip yet.
        The second source covers the 'just added, no data yet' case so
        a freshly paired device shows up in the dropdown immediately."""
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        c.execute('''
            SELECT COALESCE(device_id, '') AS did,
                   COUNT(*)               AS cnt,
                   MAX(start_time)        AS last_seen
              FROM trips
             GROUP BY did
             UNION ALL
            SELECT name      AS did,
                   0         AS cnt,
                   last_seen AS last_seen
              FROM devices
             WHERE name NOT IN (
                   SELECT DISTINCT COALESCE(device_id, '') FROM trips)
             ORDER BY cnt DESC, did
        ''')
        rows = c.fetchall()
        conn.close()
        return [{'device_id': r[0], 'trip_count': r[1], 'last_seen': r[2]}
                for r in rows]

    def get_recent_trips(self, days=7, profile_id=None, device_id=None,
                          from_iso=None, to_iso=None):
        """Get recent trips, filtered by time window and optional device_id /
        profile_id."""
        where, params = _filter_clauses(days, device_id, from_iso, to_iso)
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        if profile_id:
            c.execute(f'''SELECT * FROM trips
                WHERE {where} AND profile_id = ?
                ORDER BY start_time DESC''', params + [profile_id])
        else:
            c.execute(f'''SELECT * FROM trips
                WHERE {where}
                ORDER BY start_time DESC''', params)
        
        trips = []
        for row in c.fetchall():
            trip_id = row[0]
            # Check journey membership
            c.execute("SELECT journey_id FROM journey_trips WHERE trip_id=? LIMIT 1",
                      (trip_id,))
            jrow = c.fetchone()
            trips.append({
                'id': trip_id,
                'start_time': row[1],
                'end_time': row[2],
                'distance_km': row[7],
                'duration_seconds': row[8],
                'movement_type': row[9],
                'profile_id': row[10],
                'manual_classification': row[13],
                'avg_speed_kmh': row[14],
                'max_speed_kmh': row[15],
                'device_id': row[16] if len(row) > 16 else None,
                'journey_id': jrow[0] if jrow else None,
            })
        
        conn.close()
        return trips
    
    def get_all_trips(self):
        """Get all trips"""
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        
        c.execute('SELECT * FROM trips ORDER BY start_time DESC')
        trips = []
        for row in c.fetchall():
            trips.append({
                'id': row[0],
                'start_time': row[1],
                'end_time': row[2],
                'start_lat': row[3],
                'start_lon': row[4],
                'end_lat': row[5],
                'end_lon': row[6],
                'distance_km': row[7],
                'duration_seconds': row[8],
                'movement_type': row[9],
                'profile_id': row[10],
                'received_time': row[11],
                'sync_status': row[12],
                'manual_classification': row[13],
                'avg_speed_kmh': row[14],
                'max_speed_kmh': row[15]
            })
        
        conn.close()
        return trips

    # ---- Trip type override ----

    def set_trip_type(self, trip_id, new_type):
        """Manually override a trip's movement_type and update its profile_id."""
        profiles = {p['id']: p for p in
                    ProfileManager(self.profiles_file
                                   if hasattr(self, 'profiles_file')
                                   else PROFILES_FILE).get_all_profiles()}
        profile_id = None
        for p in profiles.values():
            if p.get('type') == new_type and p.get('auto_assign'):
                profile_id = p['id']
                break
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        c.execute("UPDATE trips SET movement_type=?, profile_id=?, "
                  "manual_classification=1 WHERE id=?",
                  (new_type, profile_id, trip_id))
        conn.commit()
        conn.close()

    def split_trip(self, trip_id, point_index):
        """Split a trip at point_index.
        Points 0..point_index  -> trip A
        Points point_index+1.. -> trip B
        Original trip deleted. Returns (trip_a_id, trip_b_id).
        """
        import math as _math
        from datetime import datetime as _dt

        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()

        c.execute("SELECT * FROM trips WHERE id=?", (trip_id,))
        orig = c.fetchone()
        if not orig:
            conn.close()
            raise ValueError("Trip {} not found".format(trip_id))

        profile_id    = orig[10]
        movement_type = orig[9]
        device_id     = orig[16] if len(orig) > 16 else None
        device_hwid   = orig[17] if len(orig) > 17 else None

        c.execute("SELECT latitude, longitude, timestamp, speed_kmh "
                  "FROM trip_points WHERE trip_id=? ORDER BY id", (trip_id,))
        points = c.fetchall()
        if point_index < 1 or point_index >= len(points) - 1:
            conn.close()
            raise ValueError("point_index {} out of range 1..{}".format(
                point_index, len(points) - 2))

        def _meta(pts):
            dist_km = avg_spd = max_spd = 0.0
            speeds = []
            prev = None
            for lat, lon, ts, spd in pts:
                if spd and spd > max_spd: max_spd = spd
                if spd: speeds.append(spd)
                if prev:
                    r = (prev[0] + lat) * 0.5 * 0.01745329
                    dlat = (lat - prev[0]) * 111320.0
                    dlon = (lon - prev[1]) * 111320.0 * _math.cos(r)
                    dist_km += _math.sqrt(dlat*dlat + dlon*dlon) / 1000.0
                prev = (lat, lon)
            avg_spd = sum(speeds) / len(speeds) if speeds else 0.0
            if   max_spd >= 40.0:                    mtype = "driving"
            elif max_spd >= 32.0 and avg_spd >= 24.0: mtype = "driving"
            elif avg_spd >= 25.0:                    mtype = "driving"
            elif avg_spd >= 7.0:                     mtype = "cycling"
            else:                                    mtype = "walking"
            return dist_km, avg_spd, max_spd, mtype

        def _profile(mtype):
            for p in ProfileManager(
                    self.profiles_file if hasattr(self, 'profiles_file')
                    else PROFILES_FILE).get_all_profiles():
                if p.get('type') == mtype and p.get('auto_assign'):
                    return p['id']
            return profile_id

        def _dur(pts):
            try:
                t0 = _dt.fromisoformat(pts[0][2])
                t1 = _dt.fromisoformat(pts[-1][2])
                return max(0, int((t1 - t0).total_seconds()))
            except Exception:
                return 0

        new_ids = []
        for pts in [points[:point_index + 1], points[point_index + 1:]]:
            if not pts: continue
            dist_km, avg_spd, max_spd, mtype = _meta(pts)
            pid = _profile(mtype)
            dur_s = _dur(pts)
            c.execute("""INSERT INTO trips
                (start_time, end_time, start_lat, start_lon, end_lat, end_lon,
                 distance_km, duration_seconds, movement_type, profile_id,
                 received_time, sync_status, manual_classification,
                 avg_speed_kmh, max_speed_kmh, device_id, device_hwid)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (pts[0][2], pts[-1][2],
                 pts[0][0], pts[0][1], pts[-1][0], pts[-1][1],
                 round(dist_km, 3), dur_s, mtype, pid,
                 _dt.now().isoformat(), 'split', 0,
                 round(avg_spd, 2), round(max_spd, 2),
                 device_id, device_hwid))
            new_trip_id = c.lastrowid
            new_ids.append(new_trip_id)
            cum_km = prev = 0.0
            prev_ll = None
            for lat, lon, ts, spd in pts:
                if prev_ll:
                    r = (prev_ll[0] + lat) * 0.5 * 0.01745329
                    dlat = (lat - prev_ll[0]) * 111320.0
                    dlon = (lon - prev_ll[1]) * 111320.0 * _math.cos(r)
                    cum_km += _math.sqrt(dlat*dlat + dlon*dlon) / 1000.0
                prev_ll = (lat, lon)
                c.execute("""INSERT INTO trip_points
                    (trip_id, latitude, longitude, timestamp,
                     distance_km, speed_kmh, device_id, device_hwid)
                    VALUES (?,?,?,?,?,?,?,?)""",
                    (new_trip_id, lat, lon, ts,
                     round(cum_km, 4), spd, device_id, device_hwid))

        # Delete original AND record it in deleted_trips.json. Without
        # this, the next PicoB sync re-imports the same trip from flash
        # and the user sees a phantom duplicate next to the two split
        # halves. Same write the explicit DELETE endpoint does.
        orig_start = orig[1]
        orig_device = orig[16] if len(orig) > 16 else None
        c.execute("DELETE FROM trip_points WHERE trip_id=?", (trip_id,))
        c.execute("DELETE FROM trips WHERE id=?", (trip_id,))
        conn.commit()
        conn.close()
        try:
            data_dir = Path(self.db_path).parent
            deleted_path = data_dir / 'deleted_trips.json'
            deleted = []
            if deleted_path.exists():
                with open(deleted_path) as f:
                    deleted = json.load(f)
            deleted.append({'db_id': trip_id, 'start_time': orig_start,
                            'device_id': orig_device})
            with open(deleted_path, 'w') as f:
                json.dump(deleted, f)
        except Exception as e:
            print("WARNING: split_trip deleted_trips.json append failed:", e)
        return tuple(new_ids)

    # ---- Journey methods ----

    def create_journey(self, name):
        from datetime import datetime
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        c.execute("INSERT INTO journeys (name, created_at) VALUES (?, ?)",
                  (name, datetime.now().isoformat()))
        jid = c.lastrowid
        conn.commit()
        conn.close()
        return jid

    def rename_journey(self, journey_id, name):
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        c.execute("UPDATE journeys SET name=? WHERE id=?", (name, journey_id))
        conn.commit()
        conn.close()

    def delete_journey(self, journey_id):
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        c.execute("DELETE FROM journey_trips WHERE journey_id=?", (journey_id,))
        c.execute("DELETE FROM journeys WHERE id=?", (journey_id,))
        conn.commit()
        conn.close()

    def add_trip_to_journey(self, journey_id, trip_id):
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        c.execute("SELECT COALESCE(MAX(position),0)+1 FROM journey_trips "
                  "WHERE journey_id=?", (journey_id,))
        pos = c.fetchone()[0]
        try:
            c.execute("INSERT INTO journey_trips (journey_id, trip_id, position) "
                      "VALUES (?,?,?)", (journey_id, trip_id, pos))
            conn.commit()
        except Exception:
            pass  # already in journey
        conn.close()

    def remove_trip_from_journey(self, journey_id, trip_id):
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        c.execute("DELETE FROM journey_trips WHERE journey_id=? AND trip_id=?",
                  (journey_id, trip_id))
        conn.commit()
        conn.close()

    def get_all_journeys(self):
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        c.execute("SELECT id, name, created_at FROM journeys ORDER BY created_at DESC")
        journeys = []
        for jid, name, created_at in c.fetchall():
            # Get member trips in order
            c.execute('''SELECT t.id, t.start_time, t.end_time,
                                t.distance_km, t.duration_seconds,
                                t.movement_type, t.profile_id,
                                t.avg_speed_kmh, t.max_speed_kmh, t.device_id
                         FROM journey_trips jt
                         JOIN trips t ON t.id = jt.trip_id
                         WHERE jt.journey_id = ?
                         ORDER BY jt.position''', (jid,))
            trips = []
            total_km = 0.0
            total_s  = 0
            for row in c.fetchall():
                trips.append({
                    'id': row[0], 'start_time': row[1], 'end_time': row[2],
                    'distance_km': row[3], 'duration_seconds': row[4],
                    'movement_type': row[5], 'profile_id': row[6],
                    'avg_speed_kmh': row[7], 'max_speed_kmh': row[8],
                    'device_id': row[9],
                })
                total_km += row[3] or 0
                total_s  += row[4] or 0
            journeys.append({
                'id': jid, 'name': name, 'created_at': created_at,
                'trips': trips,
                'total_km': round(total_km, 3),
                'total_seconds': total_s,
            })
        conn.close()
        return journeys

    def get_trip_journey(self, trip_id):
        """Return the journey_id this trip belongs to, or None."""
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        c.execute("SELECT journey_id FROM journey_trips WHERE trip_id=? LIMIT 1",
                  (trip_id,))
        row = c.fetchone()
        conn.close()
        return row[0] if row else None

    def log_event(self, event_data):
        """Log an event"""
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        
        c.execute('''INSERT INTO events 
            (event_type, latitude, longitude, timestamp, movement_type, profile_id,
             received_time, device_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)''',
            (
                event_data.get('type'),
                event_data.get('lat'),
                event_data.get('lon'),
                event_data.get('timestamp'),
                event_data.get('movement_type', 'unknown'),
                event_data.get('profile_id'),
                datetime.now().isoformat(),
                event_data.get('device_id'),
            )
        )
        
        conn.commit()
        conn.close()

    # ----------------------------------------------------------------
    # Live GPS points (real-time stream from LoRa devices)
    # ----------------------------------------------------------------
    def add_live_point(self, point):
        """Insert one live GPS point.

        Required keys: lat, lon
        Optional:      ts, alt, spd, rssi, snr, source, recv_at, device_hwid

        recv_at is the server's authoritative receive time; if not provided,
        it's set to now. The Pico's timestamp goes into 'ts' (informational).

        device_hwid is the permanent hardware id from Phase 2b+ firmware.
        If absent, resolve via the devices table from 'source' (current
        renameable label).
        """
        if 'lat' not in point or 'lon' not in point:
            return None
        recv_at = point.get('recv_at') or datetime.now().isoformat()
        source  = point.get('source')
        device_hwid = point.get('device_hwid') or point.get('hwid')
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        if not device_hwid and source:
            c.execute("SELECT id FROM devices WHERE name=?", (source,))
            row = c.fetchone()
            if row:
                device_hwid = row[0]
        elif device_hwid and not source:
            # Hwid known but no name in payload — resolve via devices.
            c.execute("SELECT name FROM devices WHERE id=?", (device_hwid,))
            row = c.fetchone()
            if row:
                source = row[0]
        c.execute('''INSERT INTO live_points
            (recv_at, lat, lon, alt, spd, ts, rssi, snr, source, device_hwid)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
            (
                recv_at,
                float(point['lat']),
                float(point['lon']),
                point.get('alt'),
                point.get('spd'),
                point.get('ts'),
                point.get('rssi'),
                point.get('snr'),
                source,
                device_hwid,
            )
        )
        new_id = c.lastrowid
        conn.commit()
        conn.close()
        return new_id

    def get_recent_live_points(self, limit=200, since=None):
        """Return recent live points, newest first.

        limit: max points to return (default 200)
        since: ISO datetime string; only points with recv_at > since
        """
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        if since:
            c.execute('''SELECT id, recv_at, lat, lon, alt, spd, ts, rssi, snr, source
                         FROM live_points
                         WHERE recv_at > ?
                         ORDER BY id DESC
                         LIMIT ?''', (since, limit))
        else:
            c.execute('''SELECT id, recv_at, lat, lon, alt, spd, ts, rssi, snr, source
                         FROM live_points
                         ORDER BY id DESC
                         LIMIT ?''', (limit,))
        rows = c.fetchall()
        conn.close()
        return [
            {
                'id': r[0], 'recv_at': r[1], 'lat': r[2], 'lon': r[3],
                'alt': r[4], 'spd': r[5], 'ts': r[6],
                'rssi': r[7], 'snr': r[8], 'source': r[9],
            } for r in rows
        ]

    def get_latest_live_point(self):
        """Return the most recent live point, or None."""
        points = self.get_recent_live_points(limit=1)
        return points[0] if points else None

    def get_latest_live_per_device(self):
        """Return one most-recent live point per distinct source/device.
        Used by the map to show a marker per device when "All" is the
        selected filter."""
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        # max(id) per source — id is monotonic with recv_at since AUTOINCREMENT
        # and inserts are append-only.
        c.execute('''SELECT id, recv_at, lat, lon, alt, spd, ts, rssi, snr, source
            FROM live_points
            WHERE id IN (
                SELECT MAX(id) FROM live_points GROUP BY COALESCE(source, '')
            )
            ORDER BY recv_at DESC''')
        rows = c.fetchall()
        conn.close()
        return [
            {'id': r[0], 'recv_at': r[1], 'lat': r[2], 'lon': r[3],
             'alt': r[4], 'spd': r[5], 'ts': r[6],
             'rssi': r[7], 'snr': r[8], 'source': r[9]}
            for r in rows
        ]

    def prune_live_points(self, days=7):
        """Delete live points older than N days. Returns number deleted."""
        cutoff = (datetime.now() - timedelta(days=days)).isoformat()
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        c.execute('DELETE FROM live_points WHERE recv_at < ?', (cutoff,))
        n = c.rowcount
        conn.commit()
        conn.close()
        return n

    # ----------------------------------------------------------------
    # Chat messages (LoRa text traffic, both directions)
    # ----------------------------------------------------------------
    def add_message(self, direction, text, source=None,
                    rssi=None, snr=None, status='sent'):
        """Insert a chat message. Returns new row id.

        direction: 'rx' or 'tx'
        text:      message body (will be stored as-is)
        source:    'picoB', 'console', 'web', etc.
        status:    'sent' (rx, or tx already sent), 'pending', 'failed'
        """
        if direction not in ('rx', 'tx'):
            raise ValueError("direction must be 'rx' or 'tx'")
        recv_at = datetime.now().isoformat()
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        c.execute('''INSERT INTO messages
            (direction, text, recv_at, rssi, snr, source, status)
            VALUES (?, ?, ?, ?, ?, ?, ?)''',
            (direction, text, recv_at, rssi, snr, source, status))
        new_id = c.lastrowid
        conn.commit()
        conn.close()
        return new_id

    def get_messages(self, limit=100, since_id=None):
        """Get recent messages. Newest first.

        since_id: only return messages with id > since_id (for polling)
        """
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        if since_id is not None:
            c.execute('''SELECT id, direction, text, recv_at,
                                rssi, snr, source, status, sent_at, error
                         FROM messages
                         WHERE id > ?
                         ORDER BY id DESC
                         LIMIT ?''', (since_id, limit))
        else:
            c.execute('''SELECT id, direction, text, recv_at,
                                rssi, snr, source, status, sent_at, error
                         FROM messages
                         ORDER BY id DESC
                         LIMIT ?''', (limit,))
        rows = c.fetchall()
        conn.close()
        return [
            {
                'id': r[0], 'direction': r[1], 'text': r[2],
                'recv_at': r[3], 'rssi': r[4], 'snr': r[5],
                'source': r[6], 'status': r[7],
                'sent_at': r[8], 'error': r[9],
            } for r in rows
        ]

    def delete_message(self, msg_id):
        """Delete a single message by id. Returns True if a row was removed."""
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        c.execute('DELETE FROM messages WHERE id = ?', (msg_id,))
        deleted = c.rowcount
        conn.commit()
        conn.close()
        return deleted > 0

    def prune_messages(self, days=7):
        """Delete messages older than N days. Returns number deleted."""
        cutoff = (datetime.now() - timedelta(days=days)).isoformat()
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        c.execute('DELETE FROM messages WHERE recv_at < ?', (cutoff,))
        n = c.rowcount
        conn.commit()
        conn.close()
        return n

    def prune_unsynced_trips(self, days=7):
        """Delete trips with sync_status != 'synced' older than N days.
        Trips that successfully synced are kept indefinitely (the goal of the
        prune is to garbage-collect partial / abandoned trip records)."""
        cutoff = (datetime.now() - timedelta(days=days)).isoformat()
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        # Get the IDs first so we can also drop their trip_points
        c.execute('''SELECT id FROM trips
                     WHERE COALESCE(sync_status,'') != 'synced'
                       AND received_time < ?''', (cutoff,))
        ids = [r[0] for r in c.fetchall()]
        if ids:
            placeholder = ','.join('?' for _ in ids)
            c.execute(f'DELETE FROM trip_points WHERE trip_id IN ({placeholder})', ids)
            c.execute(f'DELETE FROM events       WHERE trip_id IN ({placeholder})', ids)
            c.execute(f'DELETE FROM trips        WHERE id      IN ({placeholder})', ids)
        n = len(ids)
        conn.commit()
        conn.close()
        return n

# ============================================================================
# FLASK WEB SERVER
# ============================================================================

app = Flask(__name__)
db = TripDatabase()
profile_mgr = ProfileManager()
message_queue = queue.Queue()

# ============================================================================
# PROFILE ENDPOINTS
# ============================================================================

@app.route('/api/profiles')
def api_profiles():
    """Get all profiles"""
    return jsonify(profile_mgr.get_all_profiles())

@app.route('/api/profiles', methods=['POST'])
def api_create_profile():
    """Create a new profile"""
    data = request.json
    profile = profile_mgr.add_profile(
        data['id'],
        data['name'],
        data['type'],
        data.get('icon', '📌'),
        data.get('color', '#666')
    )
    return jsonify(profile), 201

@app.route('/api/profiles/<profile_id>', methods=['PUT'])
def api_update_profile(profile_id):
    """Update a profile"""
    data = request.json
    profile = profile_mgr.update_profile(profile_id, **data)
    if profile:
        return jsonify(profile)
    return jsonify({'error': 'Profile not found'}), 404

@app.route('/api/profiles/<profile_id>', methods=['DELETE'])
def api_delete_profile(profile_id):
    """Delete a profile"""
    if profile_mgr.delete_profile(profile_id):
        return jsonify({'status': 'deleted'})
    return jsonify({'error': 'Profile not found'}), 404

# ============================================================================
# ADVANCED STATISTICS ENDPOINTS
# ============================================================================

def _window_args():
    """Pull the standard time-window + device filter from request.args.

    Accepts:
      days       int, default 7
      device_id  string, optional ('' or missing -> all devices)
      from       ISO datetime/date string, optional
      to         ISO datetime/date string, optional (exclusive)

    `from`/`to` together override `days`. Either alone is also valid.
    """
    return dict(
        days      = request.args.get('days', 7, type=int),
        device_id = (request.args.get('device_id') or None),
        from_iso  = (request.args.get('from')      or None),
        to_iso    = (request.args.get('to')        or None),
    )


@app.route('/api/devices')
def api_devices():
    """List distinct device_ids that have ever logged a trip, with counts
    and last-seen timestamps. Powers the device picker in the header."""
    return jsonify({'devices': db.list_devices()})


import re as _re_rename
_RENAME_VALID = _re_rename.compile(r'^[A-Za-z0-9_-]{1,15}$')

@app.route('/api/device/rename', methods=['POST'])
def api_device_rename():
    """Rename a device.

    1. Validates the new id (alphanumeric + _ -, 1..15 chars).
    2. Inserts a 'rename' row into pending_lora_ops.
    3. Polls the row until Hub_Server marks it complete or 18 s pass.

    Hub_Server runs the actual LoRa send + reads the RENAMED reply +
    runs the DB UPDATE. trip_server is just the trigger and the
    awaiting client.
    """
    data = request.json or {}
    old = (data.get('old') or '').strip()
    new = (data.get('new') or '').strip()
    if not old:
        return jsonify({'error': 'old required'}), 400
    if not _RENAME_VALID.match(new):
        return jsonify({'error': 'new must be 1-15 chars [A-Za-z0-9_-]'}), 400
    if new == old:
        return jsonify({'error': 'new == old'}), 400

    # Resolve the device's permanent hwid from its current name. The
    # wire protocol addresses devices by hwid, so without this lookup
    # there's no way to send the rename. The hwid lands in the devices
    # table on every DEVICE: announce — if the device hasn't announced
    # since the hub came up, we can't rename it. Prompt the user to
    # power-cycle so we get an announce.
    conn = sqlite3.connect(db.db_path)
    c    = conn.cursor()
    c.execute("SELECT id FROM devices WHERE name=?", (old,))
    row = c.fetchone()
    if row is None:
        conn.close()
        return jsonify({'error':
                        f'device "{old}" has not announced yet — '
                        'power-cycle it once so the hub learns its id, '
                        'then retry'}), 400
    hwid = row[0]

    payload = json.dumps({'id': hwid, 'old': old, 'new': new})
    c.execute('''INSERT INTO pending_lora_ops (op, payload, enqueued_at)
                 VALUES (?, ?, ?)''',
              ('rename', payload, datetime.now().isoformat()))
    op_id = c.lastrowid
    conn.commit()
    conn.close()

    # Poll for completion. Hub_Server worker checks ~every 1 s, then
    # the device persists + reboots (~2-4 s) and its boot announce
    # closes the loop. Worker times out at 25 s; 30 s gives slack.
    deadline = time.time() + 30
    while time.time() < deadline:
        conn = sqlite3.connect(db.db_path)
        c    = conn.cursor()
        c.execute("SELECT status, error FROM pending_lora_ops WHERE id = ?",
                  (op_id,))
        row = c.fetchone()
        conn.close()
        if row:
            status, err = row
            if status in ('success', 'failed', 'timeout'):
                return jsonify({'status': status, 'error': err,
                                'old': old, 'new': new})
        time.sleep(0.3)
    return jsonify({'status': 'timeout',
                    'error': 'no response from device'}), 504


# Per-device debounce so a UI that rapidly cycles through devices doesn't
# flood the LoRa channel. 10 s is comfortably longer than the typical
# round-trip (≈1-3 s) and short enough that a user clicking around
# meaningfully will still get a fresh position.
_ping_last_sent = {}   # hwid -> unix-ts of last enqueue
_PING_DEBOUNCE_S = 10.0

# Single global debounce for the broadcast presence probe. The chat UI
# fires this on page load and on the refresh button; one packet covers
# all devices, so we just need to keep the click rate reasonable.
_probe_last_sent = 0.0
_PROBE_DEBOUNCE_S = 5.0


@app.route('/api/devices/presence')
def api_devices_presence():
    """Return last_seen for every known device. The chat-header dots
    consume this — fading by age, hidden if never seen.

    Reads from the devices table; no LoRa traffic. The companion route
    POST /api/devices/probe optionally broadcasts WHO? to refresh
    last_seen on devices that haven't announced recently.
    """
    conn = sqlite3.connect(db.db_path)
    c    = conn.cursor()
    c.execute("SELECT name, id, last_seen, last_rssi FROM devices ORDER BY name")
    rows = c.fetchall()
    conn.close()
    return jsonify({
        'devices': [
            {'name': r[0], 'hwid': r[1], 'last_seen': r[2], 'rssi': r[3]}
            for r in rows
        ]
    })


@app.route('/api/devices/probe', methods=['POST'])
def api_devices_probe():
    """Enqueue a broadcast WHO? presence probe.

    Every device that hears it re-emits its boot-style DEVICE: announce,
    which the hub ingests via the existing rename path and which bumps
    devices.last_seen. Fire-and-forget — the response just confirms the
    op landed in the queue. UI then re-polls /api/devices/presence after
    a short wait.
    """
    global _probe_last_sent
    now_ts = time.time()
    if now_ts - _probe_last_sent < _PROBE_DEBOUNCE_S:
        return jsonify({'status': 'debounced',
                        'retry_in': round(
                            _PROBE_DEBOUNCE_S - (now_ts - _probe_last_sent), 1)})
    _probe_last_sent = now_ts

    conn = sqlite3.connect(db.db_path)
    c    = conn.cursor()
    c.execute('''INSERT INTO pending_lora_ops (op, payload, enqueued_at)
                 VALUES (?, ?, ?)''',
              ('probe', '{}', datetime.now().isoformat()))
    op_id = c.lastrowid
    conn.commit()
    conn.close()
    return jsonify({'status': 'queued', 'op_id': op_id})

@app.route('/api/device/ping', methods=['POST'])
def api_device_ping():
    """Ask a device to broadcast its current GPS fix now (out-of-cadence).

    Fire-and-forget: enqueues a 'qpos' op for Hub_Server to transmit;
    the device's reply is a normal GPS: broadcast that the live-points
    pipeline ingests automatically. The UI just needs to refresh markers
    a moment later to see the new position.
    """
    data = request.json or {}
    name = (data.get('id') or '').strip()
    if not name:
        return jsonify({'error': 'id required'}), 400

    conn = sqlite3.connect(db.db_path)
    c    = conn.cursor()
    c.execute("SELECT id FROM devices WHERE name=?", (name,))
    row = c.fetchone()
    if row is None:
        conn.close()
        return jsonify({'error':
                        f'device "{name}" has not announced yet'}), 404
    hwid = row[0]

    now_ts = time.time()
    last = _ping_last_sent.get(hwid, 0.0)
    if now_ts - last < _PING_DEBOUNCE_S:
        conn.close()
        return jsonify({'status': 'debounced',
                        'retry_in': round(_PING_DEBOUNCE_S - (now_ts - last), 1)})
    _ping_last_sent[hwid] = now_ts

    payload = json.dumps({'id': hwid, 'name': name})
    c.execute('''INSERT INTO pending_lora_ops (op, payload, enqueued_at)
                 VALUES (?, ?, ?)''',
              ('qpos', payload, datetime.now().isoformat()))
    op_id = c.lastrowid
    conn.commit()
    conn.close()
    return jsonify({'status': 'queued', 'op_id': op_id, 'hwid': hwid})


# Per-device debounce for the manual sync trigger. PicoB processes
# one RTRIPS batch (~11 trips) at a time and pauses ~5 min before
# the next; firing extra QTRIPS within that window just costs airtime.
_sync_last_pull = {}   # hwid -> unix-ts of last enqueue
_SYNC_PULL_DEBOUNCE_S = 20.0

@app.route('/api/device/sync', methods=['POST'])
def api_device_sync():
    """User-triggered sync: ask the hub to immediately send QTRIPS to a
    device, bypassing PicoB's 5-min SYNC_RETRY_MS wait. The reply flows
    through the normal sync pipeline (RTRIPS → QTRIP → RTRIP → QPTS →
    RPTS → ACK) and trips land in the DB as they finish. Fire-and-forget;
    the UI polls /api/trips a few seconds later to see new arrivals.
    """
    data = request.json or {}
    name = (data.get('id') or '').strip()
    if not name:
        return jsonify({'error': 'id required'}), 400

    conn = sqlite3.connect(db.db_path)
    c    = conn.cursor()
    c.execute("SELECT id FROM devices WHERE name=?", (name,))
    row = c.fetchone()
    if row is None:
        conn.close()
        return jsonify({'error':
                        f'device "{name}" has not announced yet'}), 404
    hwid = row[0]

    now_ts = time.time()
    last = _sync_last_pull.get(hwid, 0.0)
    if now_ts - last < _SYNC_PULL_DEBOUNCE_S:
        conn.close()
        return jsonify({'status': 'debounced',
                        'retry_in': round(_SYNC_PULL_DEBOUNCE_S - (now_ts - last), 1)})
    _sync_last_pull[hwid] = now_ts

    payload = json.dumps({'id': hwid, 'name': name})
    c.execute('''INSERT INTO pending_lora_ops (op, payload, enqueued_at)
                 VALUES (?, ?, ?)''',
              ('sync_pull', payload, datetime.now().isoformat()))
    op_id = c.lastrowid
    conn.commit()
    conn.close()
    return jsonify({'status': 'queued', 'op_id': op_id, 'hwid': hwid})


@app.route('/api/device/listen', methods=['POST'])
def api_device_listen():
    """Pair a new device.

    Inserts a 'listen' op. Hub_Server arms it (status=inflight) and
    waits for the next DEVICE:<announced_id> on the air. On capture it
    promotes the row to a 'rename' op (announced_id -> typed name) and
    runs the existing RENAME flow. We poll one row id through both
    stages — listen times out after 30 s if no announce, then rename
    has another 25 s to ack. Total wait window: 60 s.
    """
    data = request.json or {}
    new = (data.get('new') or '').strip()
    if not _RENAME_VALID.match(new):
        return jsonify({'error': 'new must be 1-15 chars [A-Za-z0-9_-]'}), 400

    payload = json.dumps({'new': new})
    conn = sqlite3.connect(db.db_path)
    c    = conn.cursor()
    c.execute('''INSERT INTO pending_lora_ops (op, payload, enqueued_at)
                 VALUES (?, ?, ?)''',
              ('listen', payload, datetime.now().isoformat()))
    op_id = c.lastrowid
    conn.commit()
    conn.close()

    deadline = time.time() + 65
    while time.time() < deadline:
        conn = sqlite3.connect(db.db_path)
        c    = conn.cursor()
        c.execute("SELECT status, error, payload FROM pending_lora_ops WHERE id = ?",
                  (op_id,))
        row = c.fetchone()
        conn.close()
        if row:
            status, err, row_payload = row
            if status in ('success', 'failed', 'timeout'):
                old = None
                try:
                    old = json.loads(row_payload).get('old')
                except Exception:
                    pass
                return jsonify({'status': status, 'error': err,
                                'old': old, 'new': new})
        time.sleep(0.3)
    return jsonify({'status': 'timeout',
                    'error': 'no device paired within 65s'}), 504


@app.route('/api/device/<name>', methods=['DELETE'])
def api_device_delete(name):
    """Hard-delete a device from the devices table — but ONLY if it has
    no records anywhere else. Used by the UI's '×' button when the user
    decides they don't want a device they just paired (typo, change of
    mind, etc). If any data exists (trips / messages / live_points) we
    refuse with 409 and the UI falls back to the soft-hide tombstone.
    """
    conn = sqlite3.connect(db.db_path)
    c    = conn.cursor()
    c.execute("SELECT COUNT(*) FROM trips       WHERE device_id=?", (name,))
    n_trips = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM messages    WHERE source=?",    (name,))
    n_msgs  = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM live_points WHERE source=?",    (name,))
    n_live  = c.fetchone()[0]
    if n_trips or n_msgs or n_live:
        conn.close()
        return jsonify({
            'status': 'has_data',
            'error': 'device has records — hide it instead',
            'counts': {'trips': n_trips, 'messages': n_msgs,
                       'live_points': n_live},
        }), 409
    c.execute("DELETE FROM devices WHERE name=?", (name,))
    rows = c.rowcount
    conn.commit()
    conn.close()
    return jsonify({'status': 'deleted', 'rows': rows})


@app.route('/api/stats')
def api_stats():
    """Get comprehensive statistics by profile (filtered by window + device)."""
    return jsonify(db.get_statistics_by_profile(**_window_args()))


@app.route('/api/stats/<profile_id>/distribution')
def api_speed_distribution(profile_id):
    """Get speed distribution histogram for profile (filtered)."""
    return jsonify(db.get_speed_distribution(profile_id, **_window_args()))


@app.route('/api/stats/<profile_id>/extremes')
def api_extremes(profile_id):
    """Get fastest and slowest trips in profile (filtered)."""
    return jsonify(db.get_fastest_slowest_trips(profile_id, **_window_args()))


@app.route('/api/stats/<profile_id>/trend')
def api_speed_trend(profile_id):
    """Get speed trend over time for profile (filtered)."""
    return jsonify(db.get_speed_trend(profile_id, **_window_args()))

# ============================================================================
# TRIP ENDPOINTS
# ============================================================================

@app.route('/api/trips')
def api_trips():
    """Get trips, filtered by the standard time window + device + profile.

    Honours the same query params as the rest of the API:
      profile_id, device_id, days OR from/to. Missing all filters returns
      the last 7 days of trips across every device, every profile.
    """
    profile_id = request.args.get('profile_id') or None
    return jsonify(db.get_recent_trips(profile_id=profile_id,
                                       **_window_args()))

@app.route('/api/trips/<int:trip_id>')
def api_trip_details(trip_id):
    """Get detailed trip information"""
    trip = db.get_trip_details(trip_id)
    if not trip:
        return jsonify({'error': 'Trip not found'}), 404
    return jsonify(trip)


@app.route('/api/trips/<int:trip_id>', methods=['DELETE'])
def api_delete_trip(trip_id):
    """Delete a trip and all its points from the DB.
    Records the trip in deleted_trips.json so sync_manager never re-imports it.
    """
    conn2 = sqlite3.connect(db.db_path)
    c2 = conn2.cursor()
    c2.execute("SELECT start_time, device_id FROM trips WHERE id=?", (trip_id,))
    row = c2.fetchone()
    if not row:
        conn2.close()
        return jsonify({'error': 'Trip not found'}), 404
    start_time, device_id = row
    c2.execute("DELETE FROM trip_points WHERE trip_id=?", (trip_id,))
    c2.execute("DELETE FROM trips WHERE id=?", (trip_id,))
    conn2.commit()
    conn2.close()
    # Record in deleted_trips.json next to the DB
    try:
        deleted_path = DATA_DIR / 'deleted_trips.json'
        deleted = []
        if deleted_path.exists():
            with open(deleted_path) as f:
                deleted = json.load(f)
        deleted.append({'db_id': trip_id, 'start_time': start_time,
                        'device_id': device_id})
        with open(deleted_path, 'w') as f:
            json.dump(deleted, f)
    except Exception as e:
        print("WARNING: deleted_trips.json update failed:", e)
    return jsonify({'deleted': trip_id})

@app.route('/api/trips/<int:trip_id>/profile', methods=['PUT'])
def api_change_trip_profile(trip_id):
    """Move trip to different profile"""
    data = request.json
    new_profile_id = data.get('profile_id')
    reason = data.get('reason', 'Manual reclassification')
    db.change_trip_profile(trip_id, new_profile_id, reason)
    trip = db.get_trip_details(trip_id)
    return jsonify(trip)


@app.route('/api/trips/<int:trip_id>/type', methods=['PUT'])
def api_set_trip_type(trip_id):
    data = request.json
    new_type = data.get('type')
    if new_type not in ('walking', 'cycling', 'driving'):
        return jsonify({'error': 'invalid type'}), 400
    db.set_trip_type(trip_id, new_type)
    return jsonify({'id': trip_id, 'type': new_type})


@app.route('/api/trips/<int:trip_id>/split', methods=['POST'])
def api_split_trip(trip_id):
    data = request.json or {}
    point_index = data.get('point_index')
    if point_index is None:
        return jsonify({'error': 'point_index required'}), 400
    try:
        ids = db.split_trip(trip_id, int(point_index))
        return jsonify({'trip_a': ids[0], 'trip_b': ids[1]})
    except ValueError as e:
        return jsonify({'error': str(e)}), 400


# ---- Journey endpoints ----

@app.route('/api/journeys', methods=['GET'])
def api_get_journeys():
    return jsonify(db.get_all_journeys())


@app.route('/api/journeys', methods=['POST'])
def api_create_journey():
    data = request.json or {}
    name = data.get('name', 'New Journey')
    jid = db.create_journey(name)
    # If trip_ids provided, add them immediately
    for tid in data.get('trip_ids', []):
        db.add_trip_to_journey(jid, tid)
    journeys = db.get_all_journeys()
    j = next((j for j in journeys if j['id'] == jid), None)
    return jsonify(j), 201


@app.route('/api/journeys/<int:journey_id>', methods=['PUT'])
def api_update_journey(journey_id):
    data = request.json or {}
    if 'name' in data:
        db.rename_journey(journey_id, data['name'])
    journeys = db.get_all_journeys()
    j = next((j for j in journeys if j['id'] == journey_id), None)
    if not j:
        return jsonify({'error': 'not found'}), 404
    return jsonify(j)


@app.route('/api/journeys/<int:journey_id>', methods=['DELETE'])
def api_delete_journey(journey_id):
    db.delete_journey(journey_id)
    return jsonify({'deleted': journey_id})


@app.route('/api/journeys/<int:journey_id>/trips', methods=['POST'])
def api_add_trip_to_journey(journey_id):
    data = request.json or {}
    trip_id = data.get('trip_id')
    if not trip_id:
        return jsonify({'error': 'trip_id required'}), 400
    db.add_trip_to_journey(journey_id, trip_id)
    journeys = db.get_all_journeys()
    j = next((j for j in journeys if j['id'] == journey_id), None)
    return jsonify(j)


@app.route('/api/journeys/<int:journey_id>/trips/<int:trip_id>',
           methods=['DELETE'])
def api_remove_trip_from_journey(journey_id, trip_id):
    db.remove_trip_from_journey(journey_id, trip_id)
    return jsonify({'removed': trip_id})


@app.route('/api/recent')
def api_recent():
    """Get recent trips (window + device filtered)."""
    profile_id = request.args.get('profile_id', None)
    trips = db.get_recent_trips(profile_id=profile_id, **_window_args())
    return jsonify(trips)

# ============================================================================
# LIVE GPS POINT ENDPOINTS (LoRa stream)
# ============================================================================

@app.route('/api/live_point', methods=['POST'])
def api_live_point():
    """Accept a single live GPS point from the splitter.

    Body (JSON):
        {"lat":50.07,"lon":14.43,"alt":205.3,"spd":4.2,
         "ts":1730290015,"rssi":-60,"snr":9.5,"recv_at":"...","source":"picoB"}

    Required: lat, lon. All other fields optional.
    """
    data = request.json or {}
    if 'lat' not in data or 'lon' not in data:
        return jsonify({'error': 'lat and lon required'}), 400
    try:
        new_id = db.add_live_point(data)
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    if new_id is None:
        return jsonify({'error': 'insert failed'}), 500
    return jsonify({'id': new_id, 'status': 'ok'})


@app.route('/api/live')
def api_live():
    """Get recent live GPS points.

    Query params:
        limit  - max points to return (default 200)
        since  - ISO datetime string (only newer points)
        latest - if '1', return only the single most recent point
    """
    if request.args.get('latest_per_device') == '1':
        return jsonify({'latests': db.get_latest_live_per_device()})

    if request.args.get('latest') == '1':
        latest = db.get_latest_live_point()
        return jsonify({'latest': latest})

    limit = request.args.get('limit', 200, type=int)
    since = request.args.get('since', None)
    points = db.get_recent_live_points(limit=limit, since=since)
    return jsonify({'points': points, 'count': len(points)})


# ============================================================================
# ACTIVITY ENDPOINT — tail of Hub_Server's picoA_serial.log
# ============================================================================

@app.route('/api/activity')
def api_activity():
    """Return the last N lines of Hub_Server's picoA_serial.log so the Web
    UI can show a unified GPS / SYNC / RX / TX activity feed.

    Query params:
        lines    int, max=500, default=200
        filter   optional substring; if set, only matching lines returned
        device   optional device name; if set, only lines mentioning that
                 device (by current name OR hwid) survive — AND with filter
    """
    n = max(1, min(500, request.args.get('lines', 200, type=int)))
    flt = request.args.get('filter', '').strip()
    dev = request.args.get('device', '').strip()
    if not HUB_SERVER_LOG.exists():
        return jsonify({'lines': [], 'error': 'log not found',
                        'path': str(HUB_SERVER_LOG)})

    # Resolve device name -> (name, hwid). The hub log carries hwid in
    # most protocol prefixes (SYNC:<hwid>, QPOS:<hwid>, GPS:{...hwid...})
    # but the renameable label appears in DEVICE: announces, CHAT:<sender>:,
    # and trip JSON payloads. Match either so a single device filter
    # catches all the device's traffic.
    dev_needles = []
    if dev:
        dev_needles.append(dev.lower())
        try:
            conn = sqlite3.connect(db.db_path)
            c = conn.cursor()
            c.execute("SELECT id FROM devices WHERE name=?", (dev,))
            row = c.fetchone()
            conn.close()
            if row and row[0]:
                dev_needles.append(row[0].lower())
        except Exception:
            pass

    try:
        # Tail by reading up to last 64 KB. Plenty for ~500 typical log lines.
        with open(HUB_SERVER_LOG, 'rb') as f:
            f.seek(0, 2)
            size = f.tell()
            chunk = min(64 * 1024, size)
            f.seek(size - chunk)
            data = f.read()
        text = data.decode('utf-8', errors='replace')
        all_lines = text.splitlines()
        # Drop the first line if we landed mid-line (only when we didn't
        # read the entire file).
        if size > chunk and all_lines:
            all_lines = all_lines[1:]
        if flt:
            lo = flt.lower()
            all_lines = [ln for ln in all_lines if lo in ln.lower()]
        if dev_needles:
            all_lines = [ln for ln in all_lines
                         if any(n in ln.lower() for n in dev_needles)]
        return jsonify({'lines': all_lines[-n:],
                        'total': len(all_lines)})
    except Exception as e:
        return jsonify({'lines': [], 'error': str(e),
                        'path': str(HUB_SERVER_LOG)})


# Cache git info once at startup so /api/version is a constant-time
# dict read instead of a subprocess fork per page load. The deploy
# script restarts trip_server so this picks up the new SHA each push.
_VERSION_INFO = None
def _capture_version_info():
    import subprocess as _sp
    info = {'sha': None, 'short_sha': None, 'committed_at': None,
            'server_started_at': datetime.now().isoformat(timespec='seconds')}
    # Try git first (works in local dev where SCRIPT_DIR is inside the
    # repo). The Pi 5 deploy ships only source files — no .git — so we
    # fall back to a VERSION file that the deploy script drops next to
    # this file, containing `<sha> <committed_at_iso>` on one line.
    try:
        repo = str(SCRIPT_DIR.parent)
        info['sha']       = _sp.check_output(
            ['git', '-C', repo, 'rev-parse', 'HEAD'],
            stderr=_sp.DEVNULL, text=True).strip()
        info['short_sha'] = info['sha'][:7]
        info['committed_at'] = _sp.check_output(
            ['git', '-C', repo, 'log', '-1', '--format=%cI'],
            stderr=_sp.DEVNULL, text=True).strip()
    except Exception:
        try:
            ver_path = SCRIPT_DIR / 'VERSION'
            if ver_path.exists():
                with open(ver_path) as f:
                    line = f.read().strip()
                parts = line.split(None, 1)
                if parts:
                    info['sha'] = parts[0]
                    info['short_sha'] = parts[0][:7]
                if len(parts) > 1:
                    info['committed_at'] = parts[1]
        except Exception:
            pass
    return info


@app.route('/api/version')
def api_version():
    """Build / deploy identifier for the version badge in the page
    footer. Returns the current git short SHA, full SHA, last-commit
    ISO timestamp, and when the server process booted. UI shows the
    short SHA + server start time as a quick visual key for screenshots."""
    global _VERSION_INFO
    if _VERSION_INFO is None:
        _VERSION_INFO = _capture_version_info()
    return jsonify(_VERSION_INFO)


# ============================================================================
# CHAT MESSAGE ENDPOINTS (LoRa text traffic)
# ============================================================================

# Hard limit; LoRa packet payload (with AES padding) caps near 240 bytes.
# We enforce a comfortable margin so multi-byte UTF-8 chars don't overflow.
MAX_MESSAGE_LEN = 200


@app.route('/api/messages', methods=['GET'])
def api_messages_get():
    """Get recent chat messages, newest first.

    Query params:
        limit    - max to return (default 100)
        since_id - only return messages with id > since_id (for polling)
    """
    limit = request.args.get('limit', 100, type=int)
    since_id = request.args.get('since_id', type=int)
    msgs = db.get_messages(limit=limit, since_id=since_id)
    return jsonify({'messages': msgs, 'count': len(msgs)})


@app.route('/api/messages', methods=['POST'])
def api_messages_post():
    """Queue a chat message to be sent over LoRa.

    Body (JSON): {"text": "hello"}

    Inserts as direction=tx, source=web, status=pending. The hub.py
    process polls for pending rows and ships them out the radio.
    """
    data = request.json or {}
    text = data.get('text', '')
    if not isinstance(text, str):
        return jsonify({'error': 'text must be a string'}), 400
    text = text.strip()
    if not text:
        return jsonify({'error': 'text empty'}), 400
    if len(text.encode('utf-8')) > MAX_MESSAGE_LEN:
        return jsonify({
            'error': 'text too long',
            'max_bytes': MAX_MESSAGE_LEN
        }), 400
    try:
        new_id = db.add_message('tx', text,
                                source='web', status='pending')
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    return jsonify({'id': new_id, 'status': 'pending'})


@app.route('/api/messages/<int:msg_id>', methods=['DELETE'])
def api_messages_delete(msg_id):
    """Delete a single message by id. Idempotent: returns 200 even if the
    row was already gone, so the UI can fire-and-forget."""
    try:
        existed = db.delete_message(msg_id)
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    return jsonify({'id': msg_id, 'deleted': existed})


# ----------------------------------------------------------------------
# TRIP ingestion endpoints (called from hub.py when a TRIPSTART
# / TRIPEND message arrives over LoRa)
# ----------------------------------------------------------------------

@app.route('/api/trip', methods=['POST'])
def api_trip_post():
    """Ingest a completed trip.

    Request body (JSON) - accepts either compact Pico fields or full names:
        device_id (or 'd')             required
        start_time / sts (epoch sec or ISO string)
        end_time   / ets (epoch sec or ISO string)
        start_lat / slat
        start_lon / slon
        end_lat   / elat
        end_lon   / elon
        distance_km / km
        duration_seconds / dur
        movement_type / type           e.g. 'walking' | 'cycling' | 'driving'
        avg_speed_kmh / avg            optional
        max_speed_kmh / max            optional
        profile_id                     optional explicit override
        points                         optional list of {lat,lon,timestamp,...}

    Server picks profile_id from auto_assign:true profile of the matching
    movement_type if caller did not supply one.

    Returns: {trip_id, profile_id, device_id}
    """
    payload = request.get_json(silent=True) or {}

    # --- normalize Pico-compact field names to the full schema ---
    def _pick(*keys):
        for k in keys:
            if k in payload and payload[k] is not None:
                return payload[k]
        return None

    def _to_iso(v):
        """Accept either an epoch int/float or an ISO string. Return ISO string."""
        if v is None:
            return None
        if isinstance(v, (int, float)):
            try:
                return datetime.fromtimestamp(v).isoformat()
            except Exception:
                return None
        return str(v)

    device_id     = _pick('device_id', 'd', 'device')
    device_hwid   = _pick('device_hwid', 'hwid')
    movement_type = _pick('movement_type', 'type') or 'unknown'

    trip = {
        'start_time':       _to_iso(_pick('start_time', 'sts')),
        'end_time':         _to_iso(_pick('end_time', 'ets')),
        'start_lat':        _pick('start_lat', 'slat', 'sl'),
        'start_lon':        _pick('start_lon', 'slon', 'so'),
        'end_lat':          _pick('end_lat',   'elat', 'el'),
        'end_lon':          _pick('end_lon',   'elon', 'eo'),
        'distance_km':      _pick('distance_km', 'km', 'd_km') or 0,
        'duration_seconds': _pick('duration_seconds', 'dur') or 0,
        'movement_type':    movement_type,
        'avg_speed_kmh':    _pick('avg_speed_kmh', 'avg'),
        'max_speed_kmh':    _pick('max_speed_kmh', 'max'),
        'points':           payload.get('points', []),
    }

    if trip['start_lat'] is None or trip['start_lon'] is None:
        return jsonify({'error': 'start_lat/start_lon required'}), 400

    # --- pick profile ---
    profile_id = payload.get('profile_id')
    if not profile_id:
        prof = profile_mgr.get_default_profile_for_type(movement_type)
        if prof:
            profile_id = prof['id']

    try:
        trip_id = db.add_trip(trip, profile_id=profile_id,
                              device_id=device_id, device_hwid=device_hwid)
    except Exception as e:
        return jsonify({'error': str(e)}), 500

    print(f"[TRIP] device={device_id} type={movement_type} "
          f"profile={profile_id} dist={trip['distance_km']:.2f}km "
          f"-> trip id={trip_id}")
    return jsonify({
        'trip_id': trip_id,
        'profile_id': profile_id,
        'device_id': device_id,
        'movement_type': movement_type,
    })


@app.route('/api/trip_event', methods=['POST'])
def api_trip_event_post():
    """Log a trip-related event (TRIPSTART, MOVEMENT_STOPPED, etc.).

    Body (JSON):
        type / event_type           required, e.g. 'TRIPSTART'
        device_id (or 'd')          recommended
        lat, lon                    optional
        timestamp                   optional epoch or ISO
        movement_type               optional
        profile_id                  optional
    """
    payload = request.get_json(silent=True) or {}
    ev_type = payload.get('type') or payload.get('event_type')
    if not ev_type:
        return jsonify({'error': 'type required'}), 400

    ts = payload.get('timestamp') or payload.get('ts')
    if isinstance(ts, (int, float)):
        try:
            ts = datetime.fromtimestamp(ts).isoformat()
        except Exception:
            ts = None

    event_data = {
        'type':          ev_type,
        'lat':           payload.get('lat'),
        'lon':           payload.get('lon'),
        'timestamp':     ts,
        'movement_type': payload.get('movement_type'),
        'profile_id':    payload.get('profile_id'),
        'device_id':     payload.get('device_id') or payload.get('d') or payload.get('device'),
    }
    try:
        db.log_event(event_data)
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    print(f"[EVENT] {ev_type} device={event_data['device_id']}")
    return jsonify({'ok': True})


@app.route('/')
def index():
    """Serve main visualization page"""
    return send_from_directory('.', 'index.html')

# ============================================================================
# MAIN SERVER
# ============================================================================

def main():
    print("\n" + "="*60)
    print("TRIP DATA RECEIVER WITH ADVANCED STATISTICS")
    print("="*60)
    print(f"Database: {DATABASE_FILE}")
    print(f"Profiles: {PROFILES_FILE}")
    print("Advanced Features:")
    print("  📊 Speed distribution histogram")
    print("  🏆 Fastest/slowest trip tracking")
    print("  📈 Speed trend over time")
    print("="*60 + "\n")
    
    # Message processor thread
    def process_messages():
        while True:
            try:
                msg = message_queue.get(timeout=1)
                
                if msg.get('type') == 'TRIP_SYNC':
                    profile_id = msg.get('profile_id')
                    trip_id = db.add_trip(msg, profile_id)
                    move_type = msg.get('movement_type', 'unknown')
                    print(f"[DB] Stored trip {trip_id}: {move_type.upper()} | {msg['distance_km']:.2f}km")
                
                elif msg.get('type') in ['TRIP_START', 'TRIP_COMPLETE', 'MOVEMENT_STOPPED', 'TRIP_RESUME']:
                    db.log_event(msg)
                    move_type = msg.get('movement_type', 'unknown')
                    print(f"[LOG] Event: {msg['type']} ({move_type.upper()})")
            
            except queue.Empty:
                pass
            except Exception as e:
                print(f"[ERROR] Message processing: {e}")
    
    processor_thread = threading.Thread(target=process_messages, daemon=True)
    processor_thread.start()

    # ----- Periodic prune (live_points + messages, older than 7 days) -----
    def prune_live_loop():
        while True:
            time.sleep(3600)  # once an hour
            try:
                n = db.prune_live_points(days=7)
                if n:
                    print(f"[DB] Pruned {n} live point(s) older than 7 days")
            except Exception as e:
                print(f"[ERROR] Live prune failed: {e}")
            try:
                n = db.prune_messages(days=7)
                if n:
                    print(f"[DB] Pruned {n} message(s) older than 7 days")
            except Exception as e:
                print(f"[ERROR] Message prune failed: {e}")
            try:
                n = db.prune_unsynced_trips(days=7)
                if n:
                    print(f"[DB] Pruned {n} unsynced trip(s) older than 7 days")
            except Exception as e:
                print(f"[ERROR] Unsynced-trip prune failed: {e}")

    prune_thread = threading.Thread(target=prune_live_loop, daemon=True)
    prune_thread.start()

    # Start Flask server
    print("[Web] Starting web server on http://localhost:5000\n")
    app.run(host='0.0.0.0', port=5000, debug=False)

if __name__ == '__main__':
    main()