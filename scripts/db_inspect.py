#!/usr/bin/env python3
"""Quick read-only inspection of the Trip Hub SQLite database.

Usage:
    python scripts/db_inspect.py --db /path/to/trips.db
    DB=/path/to/trips.db python scripts/db_inspect.py --trip 168

Shows the latest trips (with point counts + sync status), the device-presence
roster, and optionally one trip's point span. Read-only; no credentials stored.
"""
import argparse
import os
import sqlite3
import sys


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default=os.environ.get("DB"),
                    help="path to trips.db (or set $DB)")
    ap.add_argument("--limit", type=int, default=12, help="how many trips")
    ap.add_argument("--trip", type=int, help="show point span for this trip id")
    args = ap.parse_args()
    if not args.db:
        sys.exit("Provide the DB path via --db or $DB")

    conn = sqlite3.connect(args.db)
    c = conn.cursor()

    print("=== latest trips ===")
    c.execute("""SELECT id, device_id, start_time, end_time, distance_km,
                        movement_type,
                        (SELECT COUNT(*) FROM trip_points tp WHERE tp.trip_id=t.id),
                        sync_status
                 FROM trips t ORDER BY id DESC LIMIT ?""", (args.limit,))
    for r in c.fetchall():
        print(f"  #{r[0]:<4} {str(r[1]):<10} {r[2]} -> {r[3]}  "
              f"{r[4]} km  {r[5]:<8} npts={r[6]:<4} {r[7]}")

    print("\n=== devices (presence) ===")
    try:
        c.execute("SELECT name, id, last_seen, last_rssi FROM devices ORDER BY name")
        for r in c.fetchall():
            rssi = "" if r[3] is None else f"  rssi={r[3]}"
            print(f"  {str(r[0]):<12} {r[1]:<18} last_seen={r[2]}{rssi}")
    except sqlite3.OperationalError as e:
        print("  (devices table:", e, ")")

    if args.trip is not None:
        print(f"\n=== trip {args.trip} point span ===")
        c.execute("""SELECT MIN(timestamp), MAX(timestamp), COUNT(*)
                     FROM trip_points WHERE trip_id=?""", (args.trip,))
        lo, hi, n = c.fetchone()
        print(f"  {n} points, {lo} -> {hi}")

    conn.close()


if __name__ == "__main__":
    main()
