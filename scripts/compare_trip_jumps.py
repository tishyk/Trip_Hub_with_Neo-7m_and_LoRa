#!/usr/bin/env python3
"""Fix-to-fix jump distribution per trip — a GPS-quality / outlier diagnostic.

For each trip id, computes the distance between consecutive fixes and reports
median / p90 / p99 and how many jumps exceed 100 m / 200 m (large jumps usually
mean a bad fix or a sync gap).

Usage:
    python scripts/compare_trip_jumps.py --db /path/to/trips.db 168 167 164
    DB=/path/to/trips.db python scripts/compare_trip_jumps.py 168
"""
import argparse
import math
import os
import sqlite3
import sys


def trip_jumps(db, trip_id):
    conn = sqlite3.connect(db)
    rows = conn.execute(
        "SELECT latitude, longitude FROM trip_points WHERE trip_id=? ORDER BY id",
        (trip_id,)).fetchall()
    conn.close()
    deltas, prev = [], None
    for lat, lon in rows:
        if prev:
            r = (prev[0] + lat) * 0.5 * 0.01745329
            dlat = (lat - prev[0]) * 111320
            dlon = (lon - prev[1]) * 111320 * math.cos(r)
            deltas.append(math.sqrt(dlat * dlat + dlon * dlon))
        prev = (lat, lon)
    return len(rows), deltas


def summary(label, n_pts, deltas):
    if not deltas:
        print(f"{label}: pts={n_pts} (no segments)")
        return
    s = sorted(deltas)
    n = len(s)
    med = s[n // 2]
    p90 = s[int(n * 0.9)]
    p99 = s[min(int(n * 0.99), n - 1)]
    over100 = sum(1 for d in deltas if d > 100)
    over200 = sum(1 for d in deltas if d > 200)
    print(f"{label}: pts={n_pts} med={med:5.0f}m p90={p90:5.0f}m p99={p99:5.0f}m "
          f"jumps>100m={over100} jumps>200m={over200}")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default=os.environ.get("DB"),
                    help="path to trips.db (or set $DB)")
    ap.add_argument("trips", nargs="+", type=int, help="trip id(s)")
    args = ap.parse_args()
    if not args.db:
        sys.exit("Provide the DB path via --db or $DB")
    for tid in args.trips:
        n_pts, deltas = trip_jumps(args.db, tid)
        summary(f"trip {tid:>4}", n_pts, deltas)


if __name__ == "__main__":
    main()
