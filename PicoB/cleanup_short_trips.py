#!/usr/bin/env python3
"""
cleanup_short_trips.py — sweep PicoB's flash, keeping only trips
longer than a minimum distance. Long trips are uploaded to Trip_Hub
via /api/trip (full metadata + every fix point); short trips are
deleted entirely from PicoB's .gps + .json files and sync_state.

Reuses usb_dump_trips.py's plumbing for the upload side.

Usage:
    python cleanup_short_trips.py                   # default min 5 km
    python cleanup_short_trips.py --min-km 5
    python cleanup_short_trips.py --port COM10 --hub http://raspberrypi.local:5000
    python cleanup_short_trips.py --dry-run         # report only
"""

import argparse
import json
import os
import subprocess
import sys
import tempfile

# Reuse the upload primitives.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import usb_dump_trips as udt


def list_trips_with_km(port):
    """Pull (tid, km) tuples for every trip on flash."""
    out = udt.mpremote([
        "exec",
        "import os, json\n"
        "rows = []\n"
        "for f in sorted(os.listdir('trips')):\n"
        "    if f.endswith('.json'):\n"
        "        tid = f[:-5]\n"
        "        try:\n"
        "            m = json.loads(open('trips/' + f).read())\n"
        "            km = m.get('km', 0) or 0\n"
        "        except Exception:\n"
        "            km = 0\n"
        "        rows.append((tid, km))\n"
        "import json\n"
        "print('---DUMP---')\n"
        "print(json.dumps(rows))\n",
    ], port)
    # mpremote may print device boot noise; pick out the line after the marker.
    marker = "---DUMP---"
    if marker in out:
        out = out.split(marker, 1)[1]
    return json.loads(out.strip().splitlines()[-1])


def delete_trips_on_device(port, tids):
    """Wipe .gps + .json + sync_state entries for these tids."""
    if not tids:
        return
    # MicroPython exec — small inline script.
    script = (
        "import os, json\n"
        "tids = " + repr(tids) + "\n"
        "try:\n"
        "    s = json.loads(open('sync_state.json').read())\n"
        "except Exception:\n"
        "    s = {}\n"
        "removed = 0\n"
        "for t in tids:\n"
        "    for ext in ('json','gps'):\n"
        "        p = 'trips/' + t + '.' + ext\n"
        "        try:\n"
        "            os.remove(p)\n"
        "        except OSError:\n"
        "            pass\n"
        "    s.pop(t, None)\n"
        "    removed += 1\n"
        "open('sync_state.json','w').write(json.dumps(s))\n"
        "print('removed', removed, 'trips')\n"
    )
    print(udt.mpremote(["exec", script], port).strip())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-km", type=float, default=5.0,
                    help="trips strictly longer than this are uploaded; the rest are deleted")
    ap.add_argument("--port", default="COM10")
    ap.add_argument("--hub",  default="http://raspberrypi.local:5000")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    print("Scanning PicoB trips on {} ...".format(args.port))
    rows = list_trips_with_km(args.port)
    keep   = [tid for tid, km in rows if km >  args.min_km]
    short  = [tid for tid, km in rows if km <= args.min_km]
    print("  total on flash : {}".format(len(rows)))
    print("  keep (> {} km) : {}".format(args.min_km, len(keep)))
    print("  short (<={} km): {} -> will be deleted".format(args.min_km, len(short)))
    print("  km histogram:")
    for tid, km in sorted(rows, key=lambda r: r[1]):
        flag = "KEEP" if km > args.min_km else "drop"
        print("    {}  km={:>7.3f}  {}".format(tid, km or 0, flag))

    if args.dry_run:
        print("\n--dry-run set, no changes made")
        return

    if keep:
        print("\nUploading {} long trips to hub...".format(len(keep)))
        # Re-use usb_dump_trips' fetch + post flow on the keepers.
        workdir = tempfile.mkdtemp(prefix="picob-keep-")
        try:
            state = udt.read_sync_state(args.port)
            successes = 0
            for tid in keep:
                print("  {} ...".format(tid))
                try:
                    meta, fixes = udt.fetch_trip(args.port, tid, workdir)
                    payload = udt.build_payload(meta, fixes)
                    new_id = udt.post_trip(args.hub, payload)
                    if new_id is not None:
                        print("    -> hub trip_id={}".format(new_id))
                        successes += 1
                        state[tid] = "confirmed"
                    else:
                        print("    -> POST failed; keeping on device")
                except Exception as e:
                    print("    FAILED: {}".format(e))
            # Persist the updated sync_state so post-success trips are
            # marked confirmed and the upcoming delete pass wipes them.
            udt.write_sync_state(args.port, state)
            print("  uploaded: {} / {}".format(successes, len(keep)))
        finally:
            for f in os.listdir(workdir):
                try: os.unlink(os.path.join(workdir, f))
                except Exception: pass
            os.rmdir(workdir)

    print("\nDeleting all processed trips from PicoB (both short + uploaded)...")
    # After uploads, both short and uploaded trips should be removed —
    # short because the user doesn't want them, uploaded because the hub
    # now owns them.
    delete_trips_on_device(args.port, keep + short)
    print("Done.")


if __name__ == "__main__":
    main()
