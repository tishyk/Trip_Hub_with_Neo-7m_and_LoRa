#!/usr/bin/env python3
"""
usb_dump_trips.py — pull trips off PicoB via USB serial (mpremote) and
POST them to Trip_Hub's /api/trip endpoint. Bypasses LoRa entirely so
trips with partial / failed RPTS sync land complete in seconds.

Usage:
    python usb_dump_trips.py                       # all unsent trips
    python usb_dump_trips.py T1778566165 T1778568523  # specific ids
    python usb_dump_trips.py --port COM10 --hub http://pi5:5000

After successful POST, marks the trip 'confirmed' in PicoB's
sync_state.json so it stops appearing in future RTRIPS batches.
"""

import argparse
import json
import os
import subprocess
import sys
import tempfile
import urllib.request


def mpremote(args, port):
    """Run mpremote and return stdout. Raises on non-zero exit."""
    cmd = ["python", "-m", "mpremote", "connect", port] + args
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    if r.returncode != 0:
        raise RuntimeError("mpremote failed: {}".format(r.stderr.strip()))
    return r.stdout


def read_sync_state(port):
    """Pull sync_state.json off the device and parse."""
    out = mpremote(["exec",
                    "print(open('sync_state.json').read())"], port)
    return json.loads(out)


def write_sync_state(port, state):
    """Push a new sync_state.json to the device. Uses a tempfile because
    mpremote cp wants a real filesystem source."""
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        json.dump(state, f)
        tmp_path = f.name
    try:
        mpremote(["cp", tmp_path, ":sync_state.json"], port)
    finally:
        os.unlink(tmp_path)


def fetch_trip(port, trip_id, workdir):
    """Pull .json + .gps for one trip into workdir. Returns (meta, fixes)."""
    json_path = os.path.join(workdir, trip_id + ".json")
    gps_path  = os.path.join(workdir, trip_id + ".gps")
    mpremote(["cp", ":trips/" + trip_id + ".json", json_path], port)
    mpremote(["cp", ":trips/" + trip_id + ".gps",  gps_path],  port)
    with open(json_path) as f:
        meta = json.load(f)
    fixes = []
    with open(gps_path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                fixes.append(json.loads(line))
            except Exception as e:
                print("  WARN skipping malformed fix: {}".format(e))
    return meta, fixes


def build_payload(meta, fixes):
    """Convert PicoB compact meta + raw fix arrays to /api/trip JSON.
    .gps lines are [ts, lat, lon, alt, spd]. Server expects 'points'
    with lat/lon/timestamp/distance_km/speed_kmh.
    """
    points = []
    for row in fixes:
        if not row or len(row) < 3:
            continue
        ts  = row[0]
        lat = row[1]
        lon = row[2]
        spd = row[4] if len(row) > 4 else 0.0
        points.append({
            "lat": lat,
            "lon": lon,
            "timestamp": ts,
            "speed_kmh": spd,
        })

    return {
        # Identity — pick the renameable name if present, else hwid.
        "device_id":   meta.get("device") or meta.get("d") or "PicoB",
        "device_hwid": meta.get("hwid"),
        # Trip lifecycle (epoch seconds; server normalises to ISO).
        "sts":  meta.get("start_ts"),
        "ets":  meta.get("end_ts"),
        "slat": meta.get("start_lat"),
        "slon": meta.get("start_lon"),
        "elat": meta.get("end_lat"),
        "elon": meta.get("end_lon"),
        "km":   meta.get("km"),
        "dur":  meta.get("dur"),
        "type": meta.get("type", "unknown"),
        "avg":  meta.get("avg"),
        "max":  meta.get("max"),
        "points": points,
    }


def post_trip(hub_url, payload):
    """POST to Trip_Hub's /api/trip. Returns the trip_id assigned by
    the server, or None on failure."""
    url = hub_url.rstrip("/") + "/api/trip"
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=body,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            resp = json.load(r)
        return resp.get("trip_id")
    except Exception as e:
        print("  POST failed: {}".format(e))
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("trips", nargs="*", help="trip ids to dump; default = all unsent")
    ap.add_argument("--port", default="COM10")
    ap.add_argument("--hub",  default="http://raspberrypi.local:5000")
    ap.add_argument("--keep-unsent", action="store_true",
                    help="don't mark trips confirmed in sync_state.json after POST")
    args = ap.parse_args()

    print("Reading sync_state.json from {} ...".format(args.port))
    state = read_sync_state(args.port)
    unsent = [tid for tid, st in state.items() if st != "confirmed"]
    print("  {} unsent trips on device".format(len(unsent)))

    target = args.trips if args.trips else unsent
    target = [t for t in target if t in state or t.startswith("T")]
    print("Will dump {} trips: {}".format(
        len(target), ", ".join(target[:5]) + ("..." if len(target) > 5 else "")))

    workdir = tempfile.mkdtemp(prefix="picob-dump-")
    try:
        successes = 0
        for tid in target:
            print("\n=== {} ===".format(tid))
            try:
                meta, fixes = fetch_trip(args.port, tid, workdir)
                print("  meta: km={} type={} avg={} max={} fixes={}".format(
                    meta.get("km"), meta.get("type"),
                    meta.get("avg"), meta.get("max"),
                    len(fixes)))
                payload = build_payload(meta, fixes)
                new_id = post_trip(args.hub, payload)
                if new_id is not None:
                    print("  -> hub trip_id={}".format(new_id))
                    successes += 1
                    if not args.keep_unsent:
                        state[tid] = "confirmed"
                else:
                    print("  -> POST failed; leaving as unsent")
            except Exception as e:
                print("  FAILED: {}".format(e))

        if successes and not args.keep_unsent:
            print("\nUpdating sync_state.json on device ({} trips marked confirmed) ...".format(successes))
            write_sync_state(args.port, state)
        print("\nDone. {} / {} trips POSTed.".format(successes, len(target)))
    finally:
        for f in os.listdir(workdir):
            try: os.unlink(os.path.join(workdir, f))
            except Exception: pass
        os.rmdir(workdir)


if __name__ == "__main__":
    main()
