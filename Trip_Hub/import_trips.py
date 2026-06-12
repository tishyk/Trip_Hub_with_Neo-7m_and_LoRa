#!/usr/bin/env python3
"""
import_trips.py - import trip files from Pico B into Pi 5's SQLite DB.

Workflow:
    1. On Pi 5, run from project root:
         mpremote cp -r :trips ./incoming/trips
       This pulls the Pico's trips folder to ./incoming/trips/
    2. Then run this script:
         python3 import_trips.py
       It reads each T<id>.json + T<id>.gps pair and inserts into the DB.

Idempotent: keeps a record of imported Pico trip IDs in
    ./incoming/.imported.json
so re-running the same files does NOT create duplicate trips.

If you delete a trip from the DB and want to re-import, also remove its
entry from .imported.json (or just delete the file to re-import all).

Configuration: by default uses the same DB path as the receiver
(~/trip_data/trips.db).  Override with --db <path> if needed.
"""

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone


DEFAULT_DB        = os.path.expanduser("~/trip_data/trips.db")
DEFAULT_INCOMING  = "./incoming/trips"
IMPORTED_INDEX    = ".imported.json"   # relative to incoming dir


def _to_iso(epoch_ts):
    """Convert epoch seconds (UTC, GPS-derived) to ISO string in local time
    so the web UI shows times in the user's TZ.  Pi 5's OS TZ is assumed
    correct (Europe/Prague gives DST handling for free)."""
    if epoch_ts is None:
        return None
    try:
        # Treat ts as UTC epoch, convert to local for display
        return datetime.fromtimestamp(int(epoch_ts)).isoformat()
    except Exception:
        return None


def load_imported_index(incoming_dir):
    """Load the set of already-imported Pico trip IDs."""
    path = os.path.join(incoming_dir, IMPORTED_INDEX)
    if not os.path.exists(path):
        return {}
    try:
        with open(path) as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return {}


def save_imported_index(incoming_dir, idx):
    path = os.path.join(incoming_dir, IMPORTED_INDEX)
    try:
        with open(path, "w") as f:
            json.dump(idx, f, indent=2)
    except Exception as e:
        print(f"  WARNING: could not save imported index: {e}")


def list_trip_files(incoming_dir):
    """Return list of (trip_id, json_path, gps_path) tuples for all trips
    in incoming_dir.  Skips files where one of the pair is missing."""
    if not os.path.isdir(incoming_dir):
        return []
    by_id = {}
    for name in sorted(os.listdir(incoming_dir)):
        if name.startswith("."):
            continue
        if "." not in name:
            continue
        base, ext = name.rsplit(".", 1)
        if not base.startswith("T"):
            continue
        if ext not in ("json", "gps"):
            continue
        by_id.setdefault(base, {})[ext] = os.path.join(incoming_dir, name)
    out = []
    for tid in sorted(by_id):
        if "json" in by_id[tid] and "gps" in by_id[tid]:
            out.append((tid, by_id[tid]["json"], by_id[tid]["gps"]))
    return out


def read_meta(json_path):
    try:
        with open(json_path) as f:
            return json.load(f)
    except Exception as e:
        print(f"  ERROR reading {json_path}: {e}")
        return None


def read_fixes(gps_path):
    """Yield each fix as a list [ts, lat, lon, alt, spd]."""
    try:
        with open(gps_path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    arr = json.loads(line)
                    if isinstance(arr, list) and len(arr) >= 3:
                        yield arr
                except Exception:
                    continue
    except OSError as e:
        print(f"  ERROR reading {gps_path}: {e}")


DEFAULT_PROFILES_JSON = os.path.expanduser("~/trip_data/profiles.json")


def get_default_profile_id(profiles_path, movement_type):
    """Pick the profile with auto_assign=true matching this movement_type.

    Profiles live in a JSON file (~/trip_data/profiles.json by default),
    NOT in the SQLite database.  The file format:
        {"profiles": [
            {"id": "...", "name": "...", "type": "walking|cycling|auto",
             "auto_assign": true|false, ...},
            ...
        ]}

    Returns the matching id string, or None if no match.
    """
    if not movement_type:
        return None
    try:
        with open(profiles_path) as f:
            data = json.load(f)
    except Exception:
        return None
    profiles = data.get("profiles") if isinstance(data, dict) else data
    if not profiles:
        return None
    for p in profiles:
        if (p.get("type") == movement_type
                and p.get("auto_assign") is True):
            return p.get("id")
    return None


def import_trip(conn, meta, gps_path, profiles_path):
    """Insert one trip + its points.  Returns (db_trip_id, n_points)."""
    c = conn.cursor()

    movement_type = meta.get("type") or "unknown"
    device_id     = meta.get("device")
    start_ts      = meta.get("start_ts")
    end_ts        = meta.get("end_ts")

    profile_id = get_default_profile_id(profiles_path, movement_type)

    c.execute('''INSERT INTO trips
        (start_time, end_time, start_lat, start_lon, end_lat, end_lon,
         distance_km, duration_seconds, movement_type, profile_id,
         received_time, sync_status, manual_classification, avg_speed_kmh,
         max_speed_kmh, device_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
        (
            _to_iso(start_ts),
            _to_iso(end_ts),
            meta.get("start_lat"),
            meta.get("start_lon"),
            meta.get("end_lat"),
            meta.get("end_lon"),
            meta.get("km"),
            meta.get("dur"),
            movement_type,
            profile_id,
            datetime.now().isoformat(),
            'synced',
            0,
            meta.get("avg"),
            meta.get("max"),
            device_id,
        )
    )
    db_trip_id = c.lastrowid

    # Insert all fix points
    n_points = 0
    prev_lat = prev_lon = None
    cumulative_km = 0.0
    for fix in read_fixes(gps_path):
        ts  = fix[0]
        lat = fix[1]
        lon = fix[2]
        spd = fix[4] if len(fix) > 4 and fix[4] is not None else 0.0

        # Track cumulative distance per point (matches the schema's
        # distance_km field which means "distance from trip start to here")
        if prev_lat is not None:
            cumulative_km += _approx_distance_km(prev_lat, prev_lon, lat, lon)
        prev_lat, prev_lon = lat, lon

        c.execute('''INSERT INTO trip_points
            (trip_id, latitude, longitude, timestamp, distance_km, speed_kmh, device_id)
            VALUES (?, ?, ?, ?, ?, ?, ?)''',
            (db_trip_id, lat, lon, _to_iso(ts), round(cumulative_km, 4),
             spd, device_id)
        )
        n_points += 1

    conn.commit()
    return db_trip_id, n_points


def _approx_distance_km(lat1, lon1, lat2, lon2):
    """Equirectangular approximation, kilometers."""
    import math
    avg_lat_rad = (lat1 + lat2) * 0.5 * 0.0174532925
    cos_lat = math.cos(avg_lat_rad)
    dlat = (lat2 - lat1) * 111320.0
    dlon = (lon2 - lon1) * 111320.0 * cos_lat
    return math.sqrt(dlat*dlat + dlon*dlon) / 1000.0


def main():
    ap = argparse.ArgumentParser(description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=DEFAULT_DB,
        help="Path to trips.db (default: %(default)s)")
    ap.add_argument("--incoming", default=DEFAULT_INCOMING,
        help="Folder containing T<id>.json + T<id>.gps from Pico B "
             "(default: %(default)s)")
    ap.add_argument("--profiles", default=DEFAULT_PROFILES_JSON,
        help="Path to profiles.json (default: %(default)s)")
    ap.add_argument("--force", action="store_true",
        help="Re-import all trips even if already imported")
    ap.add_argument("--dry-run", action="store_true",
        help="List what would be imported without writing")
    args = ap.parse_args()

    if not os.path.exists(args.db):
        print(f"ERROR: DB not found at {args.db}")
        print("Make sure receiver_pi5_advanced.py has been run at least "
              "once to create the schema.")
        sys.exit(1)

    if not os.path.exists(args.profiles):
        print(f"WARNING: profiles file not found at {args.profiles}")
        print("Imported trips will have profile_id=NULL.")
        print("They will appear in the DB but not under any profile tab "
              "until you assign one.")

    if not os.path.isdir(args.incoming):
        print(f"ERROR: incoming dir not found at {args.incoming}")
        print("Pull the Pico's trips folder first:")
        print(f"  mkdir -p {os.path.dirname(args.incoming) or '.'}")
        print(f"  mpremote cp -r :trips {args.incoming}")
        sys.exit(1)

    files = list_trip_files(args.incoming)
    if not files:
        print(f"No T*.json + T*.gps pairs found in {args.incoming}")
        sys.exit(0)

    imported = load_imported_index(args.incoming)
    print(f"Found {len(files)} trip pairs.")
    print(f"Already imported: {len(imported)}")

    conn = sqlite3.connect(args.db)
    n_new = n_skipped = n_failed = 0
    new_index = dict(imported)

    for tid, json_path, gps_path in files:
        if tid in imported and not args.force:
            print(f"  [skip] {tid} already imported as DB id={imported[tid]['db_id']}")
            n_skipped += 1
            continue

        meta = read_meta(json_path)
        if meta is None:
            print(f"  [fail] {tid} bad JSON, skipping")
            n_failed += 1
            continue

        if not meta.get("end_ts"):
            print(f"  [skip] {tid} not closed yet (no end_ts), skipping")
            n_skipped += 1
            continue

        if args.dry_run:
            print(f"  [DRY] would import {tid} type={meta.get('type')} "
                  f"km={meta.get('km')} dur={meta.get('dur')}s")
            n_new += 1
            continue

        try:
            db_id, n_points = import_trip(conn, meta, gps_path, args.profiles)
            new_index[tid] = {
                "db_id": db_id,
                "imported_at": datetime.now().isoformat(),
                "n_points": n_points,
            }
            print(f"  [ok]   {tid} -> DB id={db_id}  "
                  f"type={meta.get('type')}  points={n_points}  "
                  f"km={meta.get('km')}")
            n_new += 1
        except Exception as e:
            print(f"  [fail] {tid} {e}")
            n_failed += 1

    conn.close()

    if not args.dry_run:
        save_imported_index(args.incoming, new_index)

    print()
    print(f"Summary: {n_new} new, {n_skipped} skipped, {n_failed} failed")
    if args.dry_run:
        print("(dry run - nothing written)")


if __name__ == "__main__":
    main()