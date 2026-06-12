#!/usr/bin/env python3
"""
recover_trip_from_log.py — reconstruct a trip from RTRIP + RPTS log
lines and POST it to the local Trip_Hub.

Use case: hub DB rows were deleted, PicoB's flash files are gone,
but picoA_serial.log on the Pi 5 captured every RTRIP/RPTS the
device sent. This script parses those out, decodes the delta-
compressed RPTS batches with the existing sync_codec, and rebuilds
a complete trip payload (metadata + every fix) for /api/trip.

Usage (run on Pi 5):
    python3 recover_trip_from_log.py T1778175500 \\
        --log logs/picoA_serial.log \\
        --hub http://localhost:5000
"""

import argparse
import json
import os
import re
import sys
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import sync_codec  # local Hub_Server module — decode_rpts


# Lines look like:
#   2026-05-07 21:42:01 RX  RTRIP:T1778175500:{...json...}  [RSSI=...]
#   2026-05-07 21:42:03 RX  RPTS:T1778175500:0:[...encoded...]  [RSSI=...]
RTRIP_RE = re.compile(r"RX\s+RTRIP:(T\d+):(\{.*\})\s*\[RSSI=")
RPTS_RE  = re.compile(r"RX\s+RPTS:(T\d+):(\d+):(\[.*\])\s*\[RSSI=")


def parse_log(path, trip_id):
    meta = None
    # batches[from_idx] = list of fixes (keep first decoded version).
    batches = {}
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            if trip_id not in line:
                continue
            m = RTRIP_RE.search(line)
            if m and m.group(1) == trip_id:
                if meta is None:
                    try:
                        meta = json.loads(m.group(2))
                    except Exception as e:
                        print("  meta parse: {}".format(e), file=sys.stderr)
                continue
            m = RPTS_RE.search(line)
            if m and m.group(1) == trip_id:
                from_idx = int(m.group(2))
                if from_idx in batches:
                    continue   # already decoded — skip duplicate retransmit
                fixes = sync_codec.decode_rpts(m.group(3))
                if fixes:
                    batches[from_idx] = fixes
    if not meta:
        return None, []
    # Concatenate in order of from_idx — first absolute, rest deltas-decoded.
    ordered = sorted(batches.items())
    flat = []
    for _idx, fixes in ordered:
        flat.extend(fixes)
    return meta, flat


def build_payload(meta, fixes):
    points = []
    for row in fixes:
        if not row or len(row) < 3:
            continue
        points.append({
            "lat": row[1],
            "lon": row[2],
            "timestamp": row[0],
            "speed_kmh": row[4] if len(row) > 4 else 0.0,
        })
    movement_type = meta.get("type", "unknown")
    if movement_type == "auto":
        movement_type = "driving"
    return {
        "device_id":   meta.get("device") or meta.get("d") or "B1",
        "device_hwid": meta.get("hwid"),
        "sts":  meta.get("start_ts") or meta.get("sts"),
        "ets":  meta.get("end_ts")   or meta.get("ets"),
        "slat": meta.get("start_lat") or meta.get("slat"),
        "slon": meta.get("start_lon") or meta.get("slon"),
        "elat": meta.get("end_lat")   or meta.get("elat"),
        "elon": meta.get("end_lon")   or meta.get("elon"),
        "km":   meta.get("km"),
        "dur":  meta.get("dur"),
        "type": movement_type,
        "avg":  meta.get("avg"),
        "max":  meta.get("max"),
        "points": points,
    }


def post(hub_url, payload):
    url = hub_url.rstrip("/") + "/api/trip"
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.load(r)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("trip_id", help="trip id, e.g. T1778175500")
    ap.add_argument("--log", default=os.path.join(HERE, "logs/picoA_serial.log"))
    ap.add_argument("--hub", default="http://localhost:5000")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    print("Parsing {} for {}".format(args.log, args.trip_id))
    meta, fixes = parse_log(args.log, args.trip_id)
    if not meta:
        print("  no RTRIP found for", args.trip_id)
        sys.exit(1)
    print("  meta: km={} type={} dur={}s avg={} max={}".format(
        meta.get("km"), meta.get("type"), meta.get("dur"),
        meta.get("avg"), meta.get("max")))
    print("  decoded {} fixes from {} unique RPTS batches".format(
        len(fixes), len(set(
            int(m.group(2)) for m in (
                RPTS_RE.search(ln)
                for ln in open(args.log, encoding="utf-8", errors="replace")
                if args.trip_id in ln
            ) if m and m.group(1) == args.trip_id))))
    if not fixes:
        print("  no decodable RPTS batches")
        sys.exit(2)
    if args.dry_run:
        print("  --dry-run; would POST", len(fixes), "fixes")
        return
    payload = build_payload(meta, fixes)
    resp = post(args.hub, payload)
    print("  -> hub trip_id={} profile_id={}".format(
        resp.get("trip_id"), resp.get("profile_id")))


if __name__ == "__main__":
    main()
