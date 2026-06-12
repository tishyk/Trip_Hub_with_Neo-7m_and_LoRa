#!/usr/bin/env python3
"""
import_backup_trips.py — read a directory of PicoB trip files
(T*.gps + T*.json pairs) and POST trips longer than --min-km to
Trip_Hub's /api/trip endpoint.

Used to restore long historical trips after a destructive cleanup
on the device + hub. Reuses build_payload + post_trip from
usb_dump_trips.py.

Usage:
    python import_backup_trips.py "C:/path/to/trips"
    python import_backup_trips.py path --min-km 10
    python import_backup_trips.py path --hub http://raspberrypi.local:5000
    python import_backup_trips.py path --dry-run
"""

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import usb_dump_trips as udt


def read_trip(folder, tid):
    """Return (meta_dict, fixes_list) for one trip id."""
    with open(os.path.join(folder, tid + ".json")) as f:
        meta = json.load(f)
    fixes = []
    with open(os.path.join(folder, tid + ".gps")) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                fixes.append(json.loads(line))
            except Exception:
                pass
    return meta, fixes


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("folder", help="directory containing T*.gps + T*.json")
    ap.add_argument("--min-km", type=float, default=10.0)
    ap.add_argument("--hub", default="http://raspberrypi.local:5000")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    # Discover trips by .json files (must have a matching .gps).
    tids = []
    for f in sorted(os.listdir(args.folder)):
        if f.endswith(".json"):
            tid = f[:-5]
            if os.path.exists(os.path.join(args.folder, tid + ".gps")):
                tids.append(tid)

    print("Scanning {} trip pairs in {}".format(len(tids), args.folder))
    keepers = []
    for tid in tids:
        try:
            meta, fixes = read_trip(args.folder, tid)
        except Exception as e:
            print("  {}: read failed: {}".format(tid, e))
            continue
        km = meta.get("km", 0) or 0
        flag = "KEEP" if km > args.min_km else "drop"
        print("  {}  km={:>7.3f}  fixes={:>4}  type={:<8} {}".format(
            tid, km, len(fixes), meta.get("type", "?"), flag))
        if km > args.min_km:
            keepers.append((tid, meta, fixes))

    print("\n{} trips above {} km".format(len(keepers), args.min_km))
    if args.dry_run or not keepers:
        return

    print("Uploading to {}".format(args.hub))
    successes = 0
    for tid, meta, fixes in keepers:
        # Legacy PicoB used 'auto' for what the modern UI calls
        # 'driving'. The Trip_Hub profile system only auto-assigns
        # known type names; translate here so the imported trip
        # lands under the Driving profile rather than 'unknown'.
        if meta.get("type") == "auto":
            meta = dict(meta, type="driving")
        payload = udt.build_payload(meta, fixes)
        new_id = udt.post_trip(args.hub, payload)
        if new_id is not None:
            print("  {} -> hub trip_id={}".format(tid, new_id))
            successes += 1
        else:
            print("  {} -> POST failed".format(tid))
    print("\nDone. {} / {} uploaded.".format(successes, len(keepers)))


if __name__ == "__main__":
    main()
