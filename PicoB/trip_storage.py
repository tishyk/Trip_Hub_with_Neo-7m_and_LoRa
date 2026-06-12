"""
trip_storage.py - Pico B trip persistence on flash.

Per trip, we keep two files in TRIPS_DIR:
    T<start_ts>.json    -- metadata (start info, end info when known)
    T<start_ts>.gps     -- one fix per line, NDJSON-style flat array

Plus a pointer file:
    in_progress.txt     -- current trip id, or empty string if none

Design goals:
    - Minimum RAM usage. We never hold a whole trip in memory.
    - Fix appends are buffered in memory (FLUSH_EVERY) and flushed to disk
      in batches.  Reboot loses up to FLUSH_EVERY fixes.
    - 1 MB folder cap, enforced on boot only.  Oldest trips get deleted first.
    - "Stale in-progress" trips (last fix > STALE_HOURS old at boot) are
      auto-closed so they don't sit zombie forever.

API for callers (TripTracker uses these):
    init()                          -- setup dirs and check for stale trips
    open_trip(trip_id, meta)        -- start a new trip file
    append_fix(fix_dict)            -- add one fix to current trip
    close_trip(meta_update)         -- finalize current trip with end info
    list_trips()                    -- ids of all trips on flash
    read_meta(trip_id)              -- read .json
    read_fixes_range(trip_id, n0, n1) -- read fix lines [n0..n1)
    fix_count(trip_id)              -- number of lines in .gps file
    delete_trip(trip_id)            -- remove both files (used by sync ack)

Self-tests run on CPython.
"""

import os
import json

# ---- config ----
# Pull tunables from config.py so the field-tunable values live in one
# place. Falls back to the in-line defaults below if config.py is
# missing or fails to import (legacy installs / first-boot recovery).
try:
    from config import (
        TRIPS_DIR, IN_PROGRESS_FILE, SYNC_STATE_FILE,
        FLUSH_EVERY, SIZE_CAP_BYTES, STALE_SECONDS,
    )
except Exception:
    TRIPS_DIR        = "trips"
    IN_PROGRESS_FILE = "in_progress.txt"
    SYNC_STATE_FILE  = "sync_state.json"
    FLUSH_EVERY      = 5
    SIZE_CAP_BYTES   = 1 * 1024 * 1024
    STALE_SECONDS    = 60 * 60

# Sync status values (kept local — they're string sentinels, not tunables)
SYNC_UNSENT    = "unsent"      # closed, not yet synced to Pi 5
SYNC_SENT      = "sent"        # RPTS sent, waiting for ACK
SYNC_CONFIRMED = "confirmed"   # ACK received, safe to delete


# ---- module state (tiny) ----
_buf       = []         # list of fix lines pending flush, max FLUSH_EVERY
_cur_id    = None       # current trip id, or None
_cur_count = 0          # number of fixes appended (in-memory + on-disk) for current trip
_sync      = {}         # in-memory mirror of sync_state.json {trip_id: status}


# ---- low-level helpers ----------------------------------------------------
def _trip_path(trip_id, ext):
    """e.g. ('T1730290015', 'json') -> 'trips/T1730290015.json'"""
    return TRIPS_DIR + "/" + trip_id + "." + ext


def _exists(path):
    try:
        os.stat(path)
        return True
    except OSError:
        return False


def _ensure_dir(path):
    try:
        os.mkdir(path)
    except OSError:
        # already exists
        pass


def _file_size(path):
    try:
        return os.stat(path)[6]
    except OSError:
        return 0


def _read_text(path):
    try:
        with open(path) as f:
            return f.read()
    except OSError:
        return ""


def _write_text(path, text):
    with open(path, "w") as f:
        f.write(text)


def _count_lines(path):
    n = 0
    try:
        with open(path) as f:
            for _ in f:
                n += 1
    except OSError:
        pass
    return n


# ---- folder size cap -----------------------------------------------------
def _trip_files():
    """List of (trip_id, total_bytes) for trips on disk, sorted by trip_id ASC."""
    out = []
    try:
        names = os.listdir(TRIPS_DIR)
    except OSError:
        return out
    seen = {}
    for n in names:
        if "." not in n:
            continue
        base, ext = n.rsplit(".", 1)
        if not base.startswith("T"):
            continue
        sz = _file_size(TRIPS_DIR + "/" + n)
        if base in seen:
            seen[base] += sz
        else:
            seen[base] = sz
    ids = list(seen.keys())
    ids.sort()
    for tid in ids:
        out.append((tid, seen[tid]))
    return out


def _enforce_size_cap():
    """Delete oldest trips until total folder size is under SIZE_CAP_BYTES."""
    files = _trip_files()
    total = sum(sz for _, sz in files)
    while total > SIZE_CAP_BYTES and files:
        tid, sz = files.pop(0)
        delete_trip(tid)
        total -= sz


# ---- public API ----------------------------------------------------------
def init():
    """Call once at startup.  Creates dirs, prunes oversized folder.

    Does NOT auto-close any in_progress trip on its own.  The caller
    (typically the trip tracker) should call try_resume(first_new_fix)
    when the GPS lock provides a valid UTC timestamp - that's when we
    can responsibly decide "is this trip continuable, or genuinely
    abandoned?"

    Returns:
        {"in_progress": <trip_id or None>,
         "trips": <count>,
         "bytes": <int>}
    """
    _ensure_dir(TRIPS_DIR)
    _enforce_size_cap()

    in_prog = _read_text(IN_PROGRESS_FILE).strip()
    if in_prog:
        # Make sure files actually exist (filesystem might be partial)
        gps_path  = _trip_path(in_prog, "gps")
        json_path = _trip_path(in_prog, "json")
        if not (_exists(gps_path) and _exists(json_path)):
            # Stale pointer to non-existent files
            _write_text(IN_PROGRESS_FILE, "")
            in_prog = ""

    files = _trip_files()
    return {
        "in_progress": in_prog or None,
        "trips":       len(files),
        "bytes":       sum(sz for _, sz in files),
    }


# ---- Resume / close decision -----------------------------------------
RESUME_GAP_S         = 3 * 60     # max time gap (seconds) for resuming
RESUME_MAX_KMH       = 200.0      # plausible-distance speed cap
RESUME_NOISE_M       = 30.0       # GPS noise tolerance


def try_resume(first_new_fix):
    """Decide what to do with the in_progress trip given the first fresh
    GPS fix on boot.

    first_new_fix is a dict with at least 'lat', 'lon', and 'ts' (the
    GPS-UTC timestamp - caller must wait for a fix that has GPS time,
    NOT Pico clock).

    Returns one of:
        ("resume", trip_id, meta_dict, npts)  -- continue the existing trip
        ("close",  trip_id)                    -- old trip auto-closed, new fix
                                                  starts a new trip
        ("none",   None)                       -- no in_progress trip to handle

    On "close", the .json metadata is updated with end info using the
    last logged fix as end coordinates and an avg-speed-based type.
    """
    global _cur_id, _cur_count, _buf

    in_prog = _read_text(IN_PROGRESS_FILE).strip()
    if not in_prog:
        return ("none", None)

    gps_path  = _trip_path(in_prog, "gps")
    json_path = _trip_path(in_prog, "json")
    if not (_exists(gps_path) and _exists(json_path)):
        _write_text(IN_PROGRESS_FILE, "")
        return ("none", None)

    # Read trip's last fix to get last position + ts
    last_ts, last_lat, last_lon = _read_last_fix_full(gps_path)
    npts = _count_lines(gps_path)
    new_ts  = first_new_fix.get("ts")
    new_lat = first_new_fix.get("lat")
    new_lon = first_new_fix.get("lon")

    if last_ts is None or new_ts is None or last_lat is None or new_lat is None:
        # Insufficient data to make resume decision.  Conservative:
        # close the trip but mark it for inspection.
        _close_with_estimate(in_prog, json_path, gps_path, "boot_no_time")
        _write_text(IN_PROGRESS_FILE, "")
        return ("close", in_prog)

    gap_s = new_ts - last_ts
    if gap_s < 0:
        # Time went backward (e.g. GPS time went UTC after Pico clock).
        # Close conservatively.
        _close_with_estimate(in_prog, json_path, gps_path, "boot_time_skew")
        _write_text(IN_PROGRESS_FILE, "")
        return ("close", in_prog)

    if gap_s <= RESUME_GAP_S:
        # Within reasonable resume window.  Check if new fix is plausibly
        # within "could have moved this far".
        d_m = _approx_distance_m(last_lat, last_lon, new_lat, new_lon)
        max_d = (RESUME_MAX_KMH * gap_s * 1000.0 / 3600.0) + RESUME_NOISE_M
        if d_m <= max_d:
            # Resume the trip in-place.  Restore module state.
            _cur_id = in_prog
            _cur_count = npts
            _buf = []
            return ("resume", in_prog,
                    _read_meta(json_path), npts)

    # Gap too long OR moved too far -> abandon old trip
    _close_with_estimate(in_prog, json_path, gps_path, "boot_gap")
    _write_text(IN_PROGRESS_FILE, "")
    return ("close", in_prog)


def _close_with_estimate(trip_id, json_path, gps_path, reason):
    """Auto-close a trip on boot when we've decided it's not resumable.
    Reads the .gps file, computes basic stats, classifies by avg speed,
    writes the meta.
    """
    txt = _read_text(json_path)
    try:
        meta = json.loads(txt) if txt else {}
    except Exception:
        meta = {}
    if meta.get("end_ts"):
        return  # already closed

    # Walk the .gps file to compute distance / max speed / last fix
    last_ts = last_lat = last_lon = None
    prev_lat = prev_lon = None
    distance_km = 0.0
    max_kmh = 0.0
    npts = 0
    try:
        with open(gps_path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    arr = json.loads(line)
                except Exception:
                    continue
                if not arr or len(arr) < 3:
                    continue
                npts += 1
                ts, la, lo = arr[0], arr[1], arr[2]
                spd = arr[4] if len(arr) > 4 and arr[4] is not None else 0.0
                if prev_lat is not None:
                    distance_km += _approx_distance_m(prev_lat, prev_lon, la, lo) / 1000.0
                if spd > max_kmh:
                    max_kmh = spd
                last_ts, last_lat, last_lon = ts, la, lo
                prev_lat, prev_lon = la, lo
    except OSError:
        pass

    # If trip has too few fixes to be meaningful, just delete
    if npts < 2:
        delete_trip(trip_id)
        return

    start_ts = meta.get("start_ts") or last_ts
    duration_s = max(1, (last_ts or 0) - (start_ts or 0))
    avg_kmh = (distance_km / (duration_s / 3600.0)) if duration_s > 0 else 0.0
    if avg_kmh < 7.0:
        msg_type = "walking"
    elif avg_kmh < 25.0:
        msg_type = "cycling"
    else:
        msg_type = "driving"

    meta["end_ts"]   = last_ts
    meta["end_lat"]  = last_lat
    meta["end_lon"]  = last_lon
    meta["type"]     = msg_type
    meta["km"]       = round(distance_km, 3)
    meta["dur"]      = duration_s
    meta["avg"]      = round(avg_kmh, 2)
    meta["max"]      = round(max_kmh, 2)
    meta["closed_by"] = reason
    _write_text(json_path, json.dumps(meta))
    # Mark for sync
    mark_sync_status(trip_id, SYNC_UNSENT)


def _read_meta(path):
    txt = _read_text(path)
    if not txt:
        return None
    try:
        return json.loads(txt)
    except Exception:
        return None


def _read_last_fix_full(gps_path):
    """Return (ts, lat, lon) of the last fix in a .gps file, or (None, None, None)."""
    last = (None, None, None)
    try:
        with open(gps_path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    arr = json.loads(line)
                    if arr and len(arr) >= 3:
                        last = (arr[0], arr[1], arr[2])
                except Exception:
                    pass
    except OSError:
        pass
    return last


def _approx_distance_m(lat1, lon1, lat2, lon2):
    """Equirectangular approximation, no math import dependency required.
    Used internally so trip_storage doesn't depend on trip_tracker.
    """
    try:
        import math
        avg_lat_rad = (lat1 + lat2) * 0.5 * 0.0174532925
        cos_lat = math.cos(avg_lat_rad)
        dlat = (lat2 - lat1) * 111320.0
        dlon = (lon2 - lon1) * 111320.0 * cos_lat
        return math.sqrt(dlat * dlat + dlon * dlon)
    except Exception:
        # Math missing - rough fallback (tens-of-meters accuracy)
        return abs((lat2 - lat1) * 111320.0) + abs((lon2 - lon1) * 80000.0)


def open_trip(trip_id, meta):
    """Start a new trip on disk.  meta is a dict of starter info.
    Returns trip_id."""
    global _cur_id, _cur_count, _buf
    _flush_buf()
    _cur_id = trip_id
    _cur_count = 0
    _buf = []
    _write_text(_trip_path(trip_id, "json"), json.dumps(meta))
    # Create empty .gps
    with open(_trip_path(trip_id, "gps"), "w") as f:
        pass
    _write_text(IN_PROGRESS_FILE, trip_id)
    return trip_id


def append_fix(fix):
    """Append one fix to current trip.  fix is a list/tuple [ts,lat,lon,alt,spd]
    or a dict with those keys.  Buffered; flushed every FLUSH_EVERY."""
    global _cur_count, _buf
    if _cur_id is None:
        return False
    if isinstance(fix, dict):
        line = [fix.get("ts"),
                fix.get("lat"),
                fix.get("lon"),
                fix.get("alt"),
                fix.get("spd")]
    else:
        line = list(fix)
    _buf.append(line)
    _cur_count += 1
    if len(_buf) >= FLUSH_EVERY:
        _flush_buf()
    return True


def close_trip(meta_update):
    """Finalize the current trip: flush buffer, merge meta_update into
    the .json file, clear in_progress.  meta_update is a dict of end info."""
    global _cur_id, _cur_count, _buf
    if _cur_id is None:
        return None
    _flush_buf()
    # Merge meta
    cur_meta = {}
    txt = _read_text(_trip_path(_cur_id, "json"))
    if txt:
        try:
            cur_meta = json.loads(txt)
        except Exception:
            cur_meta = {}
    cur_meta.update(meta_update)
    _write_text(_trip_path(_cur_id, "json"), json.dumps(cur_meta))
    closed_id = _cur_id
    _cur_id = None
    _cur_count = 0
    _buf = []
    _write_text(IN_PROGRESS_FILE, "")
    # Mark as unsent so sync protocol picks it up
    mark_sync_status(closed_id, SYNC_UNSENT)
    return closed_id


def _flush_buf():
    """Write buffered fixes to the .gps file."""
    global _buf
    if _cur_id is None or not _buf:
        _buf = []
        return
    path = _trip_path(_cur_id, "gps")
    with open(path, "a") as f:
        for line in _buf:
            f.write(json.dumps(line, separators=(",", ":")))
            f.write("\n")
    _buf = []


def fix_count(trip_id):
    """Number of fixes saved for this trip (on-disk + buffered if current)."""
    n = _count_lines(_trip_path(trip_id, "gps"))
    if trip_id == _cur_id:
        n += len(_buf)
    return n


def list_trips():
    """All trip ids on disk, sorted ascending."""
    return [tid for tid, _sz in _trip_files()]


# ---- Sync state API -------------------------------------------------------
def _load_sync():
    """Load sync_state.json into _sync dict.  Called lazily."""
    global _sync
    txt = _read_text(SYNC_STATE_FILE)
    if txt:
        try:
            _sync = json.loads(txt)
            return
        except Exception:
            pass
    _sync = {}


def _save_sync():
    # Atomic write — tmp+rename — so a power loss mid-write doesn't
    # leave sync_state.json empty/partial. Without this, all trips
    # re-appear as UNSENT after recovery.
    tmp = SYNC_STATE_FILE + ".tmp"
    _write_text(tmp, json.dumps(_sync))
    try:
        os.rename(tmp, SYNC_STATE_FILE)
    except OSError:
        # MicroPython rename is non-atomic on some ports; if it fails
        # for any reason fall back to plain overwrite so we still
        # persist (just lose the atomicity guarantee).
        _write_text(SYNC_STATE_FILE, json.dumps(_sync))
        try:
            os.remove(tmp)
        except OSError:
            pass


def sync_status(trip_id):
    """Return sync status string for trip_id, or SYNC_UNSENT if unknown."""
    if not _sync:
        _load_sync()
    return _sync.get(trip_id, SYNC_UNSENT)


def mark_sync_status(trip_id, status):
    """Update sync status for one trip and persist to flash."""
    global _sync
    if not _sync and _read_text(SYNC_STATE_FILE):
        _load_sync()
    _sync[trip_id] = status
    _save_sync()


def clear_sync_status(trip_id):
    """Drop a trip_id's sync_state entry entirely. Used right after an
    ACK deletes the trip's files — no point keeping a dead entry
    around."""
    global _sync
    if not _sync and _read_text(SYNC_STATE_FILE):
        _load_sync()
    if trip_id in _sync:
        del _sync[trip_id]
        _save_sync()


def get_unsent_trips():
    """Return list of trip_ids that are closed but not yet confirmed synced.

    Includes SYNC_UNSENT and SYNC_SENT (sent but no ACK yet — retry on next
    session). Excludes SYNC_CONFIRMED.

    Returns list sorted **newest first** (descending). The RTRIPS reply
    is capped to ~200 B, so the order decides which trips actually fit
    in the next batch. Newest-first means recent activity reaches the
    hub fast even if older trips are stuck (e.g. corrupt .gps files
    that fail the QPTS round-trip and get retried forever).
    """
    if not _sync:
        _load_sync()
    trips_on_disk = set(list_trips())
    out = []
    for tid in sorted(trips_on_disk, reverse=True):
        status = _sync.get(tid, SYNC_UNSENT)
        if status != SYNC_CONFIRMED:
            out.append(tid)
    return out


def trip_npts(trip_id):
    """Return number of fix lines in a trip's .gps file."""
    return _count_lines(_trip_path(trip_id, "gps"))


def read_meta(trip_id):
    txt = _read_text(_trip_path(trip_id, "json"))
    if not txt:
        return None
    try:
        return json.loads(txt)
    except Exception:
        return None


def read_fixes_range(trip_id, start, count):
    """Read up to `count` fixes starting at index `start` (0-based).
    Returns list of decoded fix lists.  RAM-safe: reads line by line,
    skips before start, accumulates only the requested window."""
    out = []
    if count <= 0:
        return out
    if trip_id == _cur_id:
        # If we're asked about the current trip, flush first so the on-disk
        # file is fully up-to-date.
        _flush_buf()
    path = _trip_path(trip_id, "gps")
    if not _exists(path):
        return out
    idx = 0
    end = start + count
    try:
        with open(path) as f:
            for line in f:
                if idx >= start and idx < end:
                    line = line.strip()
                    if line:
                        try:
                            out.append(json.loads(line))
                        except Exception:
                            pass
                idx += 1
                if idx >= end:
                    break
    except OSError:
        pass
    return out


def _read_last_fix_ts(gps_path):
    """Return the ts of the last fix in a .gps file, or None."""
    last = None
    try:
        with open(gps_path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    arr = json.loads(line)
                    if arr and arr[0]:
                        last = arr[0]
                except Exception:
                    pass
    except OSError:
        return None
    return last


def delete_trip(trip_id):
    """Remove both files for a trip.  Used by sync ack (round 2) and
    size-cap enforcement at boot."""
    for ext in ("json", "gps"):
        p = _trip_path(trip_id, ext)
        try:
            os.remove(p)
        except OSError:
            pass


def current_trip_id():
    return _cur_id


# ============================================================
# Self-tests (CPython)
# ============================================================
if __name__ == "__main__":
    import shutil, tempfile

    def _go(workdir):
        global TRIPS_DIR, IN_PROGRESS_FILE
        os.chdir(workdir)
        TRIPS_DIR = "trips"
        IN_PROGRESS_FILE = "in_progress.txt"

        failures = 0

        # 1. init creates the trips dir
        print("\n[1] init creates dirs")
        st = init()
        ok = (st["trips"] == 0 and os.path.isdir(TRIPS_DIR))
        print("    init={}  dir_exists={}".format(st, os.path.isdir(TRIPS_DIR)),
              "OK" if ok else "FAIL")
        if not ok: failures += 1

        # 2. open_trip and append_fix
        print("\n[2] open + append")
        open_trip("T1000", {"id":"T1000","device":"B1","start_ts":1000,
                            "start_lat":50.0,"start_lon":14.0})
        for i in range(7):
            append_fix([1000+i*30, 50.0+i*0.0001, 14.0, 200, 4.0])
        # 7 fixes appended; flush every 5 means 5 on disk, 2 in buffer
        n = fix_count("T1000")
        ok = (n == 7)
        print("    fix_count after 7 appends = {} (expect 7)  {}".format(
            n, "OK" if ok else "FAIL"))
        if not ok: failures += 1

        # 3. close_trip flushes buffer
        print("\n[3] close_trip flushes")
        close_trip({"end_ts":1210, "end_lat":50.001, "end_lon":14.0,
                    "type":"walking", "km":0.07, "dur":210, "avg":1.2, "max":4.0})
        n_disk = _count_lines(_trip_path("T1000","gps"))
        ok = (n_disk == 7 and current_trip_id() is None)
        print("    on-disk lines={} cur=None  {}".format(n_disk, "OK" if ok else "FAIL"))
        if not ok: failures += 1

        # 4. read_meta returns merged data
        print("\n[4] read_meta:")
        meta = read_meta("T1000")
        ok = (meta is not None and meta["device"]=="B1"
              and meta["end_ts"]==1210 and meta["type"]=="walking")
        print("    {}  {}".format(meta, "OK" if ok else "FAIL"))
        if not ok: failures += 1

        # 5. read_fixes_range
        print("\n[5] read_fixes_range:")
        rows = read_fixes_range("T1000", 0, 3)
        ok = (len(rows) == 3 and rows[0][0] == 1000)
        print("    first 3: {}  {}".format(rows, "OK" if ok else "FAIL"))
        if not ok: failures += 1
        rows = read_fixes_range("T1000", 5, 5)
        ok = (len(rows) == 2 and rows[0][0] == 1150)
        print("    range 5..end (got {}): {}  {}".format(len(rows), rows, "OK" if ok else "FAIL"))
        if not ok: failures += 1

        # 6. list_trips
        print("\n[6] list_trips:")
        open_trip("T2000", {"id":"T2000","device":"B1","start_ts":2000,
                            "start_lat":51.0,"start_lon":15.0})
        for i in range(3):
            append_fix([2000+i*30, 51.0, 15.0, 200, 0.5])
        close_trip({"end_ts":2090,"end_lat":51.0,"end_lon":15.0,
                    "type":"walking","km":0,"dur":90,"avg":0,"max":0.5})
        ids = list_trips()
        ok = (ids == ["T1000","T2000"])
        print("    {}  {}".format(ids, "OK" if ok else "FAIL"))
        if not ok: failures += 1

        # 7. delete_trip
        print("\n[7] delete_trip:")
        delete_trip("T1000")
        ids = list_trips()
        ok = (ids == ["T2000"])
        print("    after delete: {}  {}".format(ids, "OK" if ok else "FAIL"))
        if not ok: failures += 1

        # 8. read_fixes_range gracefully handles current trip with buffered fixes
        print("\n[8] read_fixes_range on current (buffered):")
        open_trip("T3000", {"id":"T3000","device":"B1","start_ts":3000,
                            "start_lat":52.0,"start_lon":16.0})
        for i in range(3):
            append_fix([3000+i*30, 52.0, 16.0, 100, 5.0])
        # 3 in buffer, none on disk yet
        rows = read_fixes_range("T3000", 0, 5)
        ok = (len(rows) == 3)
        print("    got {} fixes (expect 3, was buffered)  {}".format(
            len(rows), "OK" if ok else "FAIL"))
        if not ok: failures += 1
        close_trip({"end_ts":3090, "type":"walking"})

        # 9. init does NOT auto-close in_progress
        print("\n[9] init preserves in_progress (no premature close):")
        # Create a trip and leave it open
        open_trip("T4000", {"id":"T4000","device":"B1","start_ts":4000,
                            "start_lat":53.0,"start_lon":17.0})
        for i in range(3):
            append_fix([4000+i*30, 53.0, 17.0, 100, 1.0])
        _flush_buf()
        # Simulate reboot
        global _cur_id, _cur_count, _buf
        _cur_id = None; _cur_count = 0; _buf = []
        st = init()
        ok = (st["in_progress"] == "T4000")
        print("    in_progress={} (expect T4000)  {}".format(
            st["in_progress"], "OK" if ok else "FAIL"))
        if not ok: failures += 1
        # Trip should NOT have end_ts (still open)
        meta = read_meta("T4000")
        ok = (meta and meta.get("end_ts") is None)
        print("    trip stays open: end_ts={}  {}".format(
            meta.get("end_ts") if meta else None, "OK" if ok else "FAIL"))
        if not ok: failures += 1

        # 10. try_resume: gap small + close distance -> resume
        print("\n[10] try_resume small gap + close distance:")
        # Last fix in T4000 is at ts=4060, lat=53.0, lon=17.0
        # New fix 30s later, 5m away -> resume
        new_fix = {"ts": 4090, "lat": 53.00005, "lon": 17.0}
        result = try_resume(new_fix)
        ok = (result[0] == "resume" and result[1] == "T4000")
        print("    result={}  {}".format(result, "OK" if ok else "FAIL"))
        if not ok: failures += 1
        # Module state should be hydrated to current trip
        ok = (current_trip_id() == "T4000")
        print("    current_trip_id() == T4000  {}".format(
            "OK" if ok else "FAIL got " + str(current_trip_id())))
        if not ok: failures += 1
        # Continue appending to T4000
        append_fix([4090, 53.00005, 17.0, 100, 1.0])
        ok = (fix_count("T4000") == 4)
        print("    fix_count after resume+append={} (expect 4)  {}".format(
            fix_count("T4000"), "OK" if ok else "FAIL"))
        if not ok: failures += 1
        close_trip({"end_ts":4090, "type":"walking", "km":0.005, "dur":90,
                    "avg":0.2, "max":1.0})

        # 11. try_resume: gap too long -> close
        print("\n[11] try_resume gap > 3min -> close:")
        open_trip("T5000", {"id":"T5000","device":"B1","start_ts":5000,
                            "start_lat":50.0,"start_lon":14.0})
        for i in range(3):
            append_fix([5000+i*30, 50.0+i*0.0001, 14.0, 100, 4.0])
        _flush_buf()
        _cur_id = None; _cur_count = 0; _buf = []
        # Reboot, new fix 5 minutes later
        new_fix = {"ts": 5000+60+300, "lat": 50.0, "lon": 14.0}
        init()
        result = try_resume(new_fix)
        ok = (result[0] == "close" and result[1] == "T5000")
        print("    result={}  {}".format(result, "OK" if ok else "FAIL"))
        if not ok: failures += 1
        meta = read_meta("T5000")
        ok = (meta and meta.get("end_ts") is not None
              and meta.get("closed_by") == "boot_gap"
              and meta.get("type") == "walking")
        print("    closed: end_ts={} closed_by={} type={}  {}".format(
            meta.get("end_ts") if meta else None,
            meta.get("closed_by") if meta else None,
            meta.get("type") if meta else None,
            "OK" if ok else "FAIL"))
        if not ok: failures += 1

        # 12. try_resume: gap small but too far away -> close
        print("\n[12] try_resume close gap but far jump -> close:")
        open_trip("T6000", {"id":"T6000","device":"B1","start_ts":6000,
                            "start_lat":50.0,"start_lon":14.0})
        for i in range(3):
            append_fix([6000+i*30, 50.0, 14.0, 100, 1.0])
        _flush_buf()
        _cur_id = None; _cur_count = 0; _buf = []
        # 30s gap, but 50km away (way past 200 km/h * 30s = 1.7km)
        new_fix = {"ts": 6060+30, "lat": 50.5, "lon": 14.0}
        init()
        result = try_resume(new_fix)
        ok = (result[0] == "close" and result[1] == "T6000")
        print("    result={}  {}".format(result, "OK" if ok else "FAIL"))
        if not ok: failures += 1

        # 13. try_resume: no in_progress -> none
        print("\n[13] try_resume with no in_progress -> none:")
        result = try_resume({"ts": 7000, "lat": 50.0, "lon": 14.0})
        ok = (result[0] == "none")
        print("    result={}  {}".format(result, "OK" if ok else "FAIL"))
        if not ok: failures += 1

        # 14. try_resume: 200 km/h drive case
        print("\n[14] try_resume 200km/h, plausible distance OK:")
        open_trip("T8000", {"id":"T8000","device":"B1","start_ts":8000,
                            "start_lat":50.0,"start_lon":14.0})
        for i in range(3):
            append_fix([8000+i*30, 50.0+i*0.001, 14.0, 100, 50.0])
        _flush_buf()
        _cur_id = None; _cur_count = 0; _buf = []
        # 30s gap, 1km away (50 km/h * 30s = 0.4km moved + 30m)
        # but at 200 km/h plausibility = 1.7km, so OK
        last_lat = 50.0 + 2*0.001
        new_fix = {"ts": 8060+30, "lat": last_lat + 0.009, "lon": 14.0}
        init()
        result = try_resume(new_fix)
        ok = (result[0] == "resume")
        print("    result={}  {}".format(result, "OK" if ok else "FAIL"))
        if not ok: failures += 1

        # ---- Sync state tests ----

        # 15. close_trip marks trip as SYNC_UNSENT
        print("\n[15] close_trip marks trip SYNC_UNSENT:")
        global _sync
        _sync = {}
        open_trip("T9000", {"id":"T9000","device":"B1","start_ts":9000,
                            "start_lat":50.0,"start_lon":14.0})
        append_fix([9000, 50.0, 14.0, 100, 1.0])
        close_trip({"end_ts":9100,"end_lat":50.001,"end_lon":14.0,
                    "type":"walking","km":0.1,"dur":100,"avg":3.6,"max":5.0,
                    "split":0})
        ok = (sync_status("T9000") == SYNC_UNSENT)
        print("    status={}  {}".format(sync_status("T9000"),
                                          "OK" if ok else "FAIL"))
        if not ok: failures += 1

        # 16. mark_sync_status + get_unsent_trips
        print("\n[16] get_unsent_trips returns unsent+sent, not confirmed:")
        _sync = {}
        open_trip("TA000", {"id":"TA000","device":"B1","start_ts":10000,
                            "start_lat":50.0,"start_lon":14.0})
        append_fix([10000, 50.0, 14.0, 100, 1.0])
        close_trip({"end_ts":10100,"end_lat":50.001,"end_lon":14.0,
                    "type":"walking","km":0.1,"dur":100,"avg":3.6,"max":5.0,
                    "split":0})
        open_trip("TB000", {"id":"TB000","device":"B1","start_ts":11000,
                            "start_lat":50.0,"start_lon":14.0})
        append_fix([11000, 50.0, 14.0, 100, 1.0])
        close_trip({"end_ts":11100,"end_lat":50.001,"end_lon":14.0,
                    "type":"walking","km":0.1,"dur":100,"avg":3.6,"max":5.0,
                    "split":0})
        mark_sync_status("TA000", SYNC_SENT)
        mark_sync_status("TB000", SYNC_CONFIRMED)
        unsent = get_unsent_trips()
        ok = ("TA000" in unsent and "TB000" not in unsent)
        print("    unsent={} (TA=sent,TB=confirmed)  {}".format(
            unsent, "OK" if ok else "FAIL"))
        if not ok: failures += 1

        # 17. trip_npts counts lines
        print("\n[17] trip_npts:")
        n = trip_npts("T9000")
        ok = (n == 1)
        print("    npts={} expect 1  {}".format(n, "OK" if ok else "FAIL"))
        if not ok: failures += 1

        # 18. sync status persists after _sync cleared (simulates reboot)
        print("\n[18] sync status persists after _sync cleared:")
        _sync = {}
        ok = (sync_status("T9000") == SYNC_UNSENT)
        print("    status after reload={}  {}".format(
            sync_status("T9000"), "OK" if ok else "FAIL"))
        if not ok: failures += 1

        print()
        if failures == 0:
            print("ALL SELF-TESTS PASSED")
        else:
            print("{} FAILURES".format(failures))
        return failures

    workdir = tempfile.mkdtemp(prefix="tripstor_")
    try:
        f = _go(workdir)
        if f != 0:
            raise SystemExit(1)
    finally:
        shutil.rmtree(workdir)