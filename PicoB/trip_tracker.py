"""
trip_tracker.py - Detect trip start/end on Pico B from a stream of GPS fixes.

Round B design:
    - Trip splits happen via STOP DETECTION, never speed-class crossings.
    - The stop-detect threshold depends on the trip's peak class so far:
        walking trip -> 120s stationary -> end
        cycling trip ->  60s stationary -> end
        auto trip    -> 300s stationary -> end
    - At trip end, classify by PEAK SUSTAINED speed over any 5-min window
      across the trip's fixes (read from disk via trip_storage).
      Short trips (< 5 min) classify by whole-trip avg.
    - Cadence selection (handled in runtime.py) uses instantaneous speed
      with hysteresis - a different concern from classification.

State machine:
    IDLE   -- no trip in progress
    MOVING -- trip in progress, accumulating distance / max speed

Transitions:
    IDLE -> MOVING when:
        speed > MOVE_SPEED_KMH sustained for >= MOVE_HOLD_S, OR
        moved more than START_DIST_M from the IDLE anchor

    MOVING -> IDLE when:
        position has not moved more than END_DIST_M for SPLIT_STOP_S seconds,
        where SPLIT_STOP_S depends on the trip's running peak class.

update() returns a list of events:
    []                    -- nothing happened this fix
    [("TRIPSTART", msg)]  -- trip just started
    [("TRIPEND",   msg)]  -- trip just ended (via stop detection)
"""

import time

try:
    import math
    _HAS_MATH = True
except Exception:
    _HAS_MATH = False

try:
    import trip_storage
    _HAS_STORAGE = True
except Exception:
    _HAS_STORAGE = False


# ---- thresholds ----------------------------------------------------------
# Pulled from config.py so field-tunable values live in one place. Falls
# back to in-line defaults if config.py is missing (legacy install).
try:
    from config import (
        MOVE_SPEED_KMH, MOVE_HOLD_S, START_DIST_M, END_DIST_M,
        SPLIT_STOP_BY_CLASS,
    )
except Exception:
    MOVE_SPEED_KMH    = 5.0
    MOVE_HOLD_S       = 30
    START_DIST_M      = 100.0
    END_DIST_M        = 15.0  # within this radius = stationary
    SPLIT_STOP_BY_CLASS = None  # gets overridden below if not loaded

# GPS-settling gate: refuse to start a trip until the receiver has
# delivered this many accepted fixes since boot. NEO-7M cold-lock
# wander (50-200 m of position drift with phantom 1-5 km/h Doppler)
# can fire both `far` and `sustained` triggers before the solution
# stabilises — waiting for stability is the only robust defense.
MIN_FIXES_BEFORE_START = 5

# Retroactive trip-start: while IDLE, buffer the most recent fixes
# that show non-trivial motion (spd >= TRIP_LOW_SPEED_KMH). When the
# trigger eventually fires (after sustained 30 s / far 100 m) the
# buffered fixes become the trip's prefix — start_lat/lon/ts come
# from the FIRST buffered fix, not the trigger fix. Without this we'd
# throw away ~30 s of valid GPS data that happened during the
# trigger's window-up time, and the recorded trip start would be
# 100-400 m downstream of where the car actually departed (the issue
# trip 138 exposed: first fix already at 12.77 km/h).
TRIP_LOW_SPEED_KMH    = 2.0
MOTION_BUFFER_MAX     = 6   # ~60 s at 10 s cadence — covers both triggers

# Post-trip sanity check: if a trip ends without confirming the
# start conditions (no meaningful distance covered AND no sustained
# speed), it was a false-start (GPS multipath bursts that happened to
# trip 'sustained' or `far` followed by a quick stop). Delete the
# .gps + .json files from flash and suppress the TRIPEND broadcast.
MIN_REAL_TRIP_M       = 100.0   # at least 100 m of actual travel
MIN_REAL_TRIP_MAX_KMH = 5.0     # OR at least one sustained 5 km/h fix

# Stop-detect threshold (seconds) by peak class of the trip so far.
# Only used when SPLIT_STOP_BY_CLASS wasn't loaded from config.py above.
if SPLIT_STOP_BY_CLASS is None:
    SPLIT_STOP_BY_CLASS = {
        "walking": 120,
        "cycling":  60,
        "driving": 300,
    }

# Rolling window for tracking trip's peak class (running max).
PEAK_WINDOW_S = 5 * 60        # 5-minute window

# Classification thresholds (km/h)
CLASS_WALK_MAX_KMH = 7.0
CLASS_BIKE_MAX_KMH = 25.0


# ---- helpers -------------------------------------------------------------
def approx_distance_m(lat1, lon1, lat2, lon2):
    """Approximate meters between two coordinates (equirectangular)."""
    avg_lat_rad = (lat1 + lat2) * 0.5 * 0.0174532925
    if _HAS_MATH:
        cos_lat = math.cos(avg_lat_rad)
    else:
        cos_lat = 1.0
    dlat = (lat2 - lat1) * 111320.0
    dlon = (lon2 - lon1) * 111320.0 * cos_lat
    if _HAS_MATH:
        return math.sqrt(dlat * dlat + dlon * dlon)
    return abs(dlat) + abs(dlon)


def classify(avg_kmh):
    if avg_kmh < CLASS_WALK_MAX_KMH:
        return "walking"
    if avg_kmh < CLASS_BIKE_MAX_KMH:
        return "cycling"
    return "driving"


def classify_with_max(avg_kmh, max_kmh):
    """Classify using both avg and max speed.

    Max speed is a more reliable signal than avg for short trips where
    city traffic stops pull the average down.  Rules:
      - max >= 40 km/h -> driving (no cyclist sustains 40+ km/h)
      - max >= 28 km/h AND avg >= 15 km/h -> driving (sustained fast)
      - otherwise fall back to avg-based classification

    Examples:
      avg=20, max=45 -> driving  (car-in-traffic case)
      avg=22, max=35 -> cycling  (fast cyclist)
      avg=12, max=55 -> driving  (stop-start city driving)
      avg=5,  max=8  -> walking
    """
    if max_kmh >= 40.0:
        return "driving"
    if max_kmh >= 32.0 and avg_kmh >= 24.0:
        return "driving"
    return classify(avg_kmh)


def _class_rank(c):
    """Rank classes so we can take running max.  walking < cycling < driving."""
    if c == "driving": return 2
    if c == "cycling": return 1
    return 0


def classify_by_peak_sustained(fixes, window_s=PEAK_WINDOW_S):
    """Find peak (median speed) over any window_s-second window in `fixes`
    and classify by that.  fixes is an iterable yielding [ts, lat, lon, alt, spd]
    arrays in chronological order.

    Median (not mean) makes this robust to single-fix speed spikes - a
    walking trip with one 30 km/h sprint stays "walking".

    For trips shorter than window_s, falls back to whole-trip median.

    Returns (movement_type, peak_median_kmh).
    """
    ts_spd = []
    for arr in fixes:
        if not arr or len(arr) < 5:
            continue
        ts = arr[0]
        spd = arr[4] if arr[4] is not None else 0.0
        if ts is not None:
            ts_spd.append((ts, spd))
    if not ts_spd:
        return ("walking", 0.0)
    if len(ts_spd) == 1:
        return (classify(ts_spd[0][1]), ts_spd[0][1])

    def _median(vals):
        if not vals:
            return 0.0
        s = sorted(vals)
        m = len(s)
        if m % 2 == 1:
            return s[m // 2]
        return (s[m//2 - 1] + s[m//2]) / 2.0

    duration = ts_spd[-1][0] - ts_spd[0][0]
    max_spd = max(s for _, s in ts_spd)
    if duration < window_s:
        med = _median([s for _, s in ts_spd])
        return (classify_with_max(med, max_spd), med)

    n = len(ts_spd)
    peak = 0.0
    j = 0
    for i in range(n):
        while j < n and (ts_spd[j][0] - ts_spd[i][0]) <= window_s:
            j += 1
        if j - i < 1:
            continue
        # Window must span at least half the requested duration to count
        if (ts_spd[j-1][0] - ts_spd[i][0]) < window_s * 0.5:
            continue
        med = _median([ts_spd[k][1] for k in range(i, j)])
        if med > peak:
            peak = med
    if peak <= 0.0:
        med = _median([s for _, s in ts_spd])
        return (classify_with_max(med, max_spd), med)
    return (classify_with_max(peak, max_spd), peak)


# =========================================================================
# TripTracker
# =========================================================================
class TripTracker:
    STATE_IDLE   = "IDLE"
    STATE_MOVING = "MOVING"

    def __init__(self, device_id="B1", use_storage=True, device_hwid=""):
        self.device_id    = device_id
        self.device_hwid  = device_hwid
        self._use_storage = use_storage and _HAS_STORAGE
        self.state        = self.STATE_IDLE
        self.trip_id      = None
        self._resumed_once = False

        # IDLE state
        self._anchor_lat    = None
        self._anchor_lon    = None
        self._fast_since_ts = 0
        # GPS-settling gate counter; see MIN_FIXES_BEFORE_START.
        self._fixes_seen    = 0
        # Ring buffer of recent IDLE fixes that showed motion. When a
        # trip-start trigger fires these become the trip's prefix so
        # the recorded start matches actual departure.
        self._motion_buffer = []

        # MOVING state
        self._trip_start_ts   = 0
        self._trip_start_lat  = 0.0
        self._trip_start_lon  = 0.0
        self._last_lat        = 0.0
        self._last_lon        = 0.0
        self._stop_anchor_lat = 0.0
        self._stop_anchor_lon = 0.0
        self._stop_since_ts   = 0     # 0 = currently moving
        self._distance_km     = 0.0
        self._max_speed_kmh   = 0.0
        # Previous fix's speed — used so a single-fix doppler spike
        # (cold-start glitch, multipath, brief lock loss) can't become
        # the recorded max. min(prev, curr) is the "sustained" speed.
        self._prev_spd        = 0.0
        self._last_fix_gps_ts = None  # GPS UTC ts of most recent fix in this trip

        # Peak-class tracking (running max as trip progresses).
        # Used to pick the stop-detect threshold.
        self._peak_class    = "walking"   # conservative default
        self._peak_window   = []          # [(ts, spd), ...] in last PEAK_WINDOW_S

    # ---- public API -----------------------------------------------------
    def in_trip(self):
        return self.state == self.STATE_MOVING

    def last_fix_gps_ts(self):
        """Return GPS UTC time of the last fix fed into this trip, or None
        if we haven't seen any fixes yet (e.g. just resumed from disk and
        no new fix has arrived)."""
        return self._last_fix_gps_ts

    def force_close(self, reason="watchdog"):
        """Force-close the current trip without a fresh fix.  Used when GPS
        signal was lost mid-trip and stop-detection can't fire because no
        fixes are arriving.

        Uses the last known fix from disk for end coordinates and end_ts.
        Returns ("TRIPEND", msg) or None if no trip is active.
        """
        if self.state != self.STATE_MOVING or not self.trip_id:
            return None

        # Determine end coords + end_ts from disk
        end_lat = self._last_lat
        end_lon = self._last_lon
        end_ts  = self._last_fix_gps_ts or self._trip_start_ts

        if self._use_storage:
            try:
                # Try to read the actual last fix from .gps file
                rows = []
                idx = 0
                batch = 50
                while True:
                    chunk = trip_storage.read_fixes_range(self.trip_id, idx, batch)
                    if not chunk:
                        break
                    rows.extend(chunk)
                    if len(chunk) < batch:
                        break
                    idx += batch
                if rows:
                    last = rows[-1]
                    if len(last) >= 3:
                        end_lat = last[1]
                        end_lon = last[2]
                        if last[0]:
                            end_ts = last[0]
            except Exception:
                pass

        msg = self._end_trip(end_lat, end_lon, end_ts,
                             closed_by=reason)
        return msg

    def boot_resume_check(self, fix):
        """Call once on boot with the first GPS fix that has a UTC timestamp.
        Returns ("resume",tid) | ("close",tid) | None.  See trip_storage.try_resume.
        """
        if self._resumed_once or not self._use_storage:
            return None
        self._resumed_once = True
        try:
            result = trip_storage.try_resume(fix)
        except Exception:
            return None
        if result[0] == "none":
            return None
        if result[0] == "close":
            return ("close", result[1])
        # resume
        _, trip_id, meta, npts = result
        self.state   = self.STATE_MOVING
        self.trip_id = trip_id
        self._trip_start_ts  = meta.get("start_ts", fix.get("ts", 0))
        self._trip_start_lat = meta.get("start_lat", fix.get("lat", 0.0))
        self._trip_start_lon = meta.get("start_lon", fix.get("lon", 0.0))
        self._last_lat = fix.get("lat", 0.0)
        self._last_lon = fix.get("lon", 0.0)
        self._stop_anchor_lat = self._last_lat
        self._stop_anchor_lon = self._last_lon
        self._stop_since_ts   = 0
        # The fresh fix becomes our "last seen" reference for the watchdog
        self._last_fix_gps_ts = fix.get("gps_ts") or fix.get("ts")
        # Recap distance/max from disk
        try:
            recap = self._recap_from_disk(trip_id)
            self._distance_km   = recap["distance_km"]
            self._max_speed_kmh = recap["max_kmh"]
            # Also recap peak class from recent fixes
            self._peak_class = recap.get("peak_class", "walking")
        except Exception:
            self._distance_km   = 0.0
            self._max_speed_kmh = 0.0
        self._peak_window = []
        return ("resume", trip_id)

    def update(self, fix):
        """Feed one GPS fix. Returns list of events (possibly empty)."""
        lat = fix.get("lat")
        lon = fix.get("lon")
        ts  = int(fix.get("ts", 0)) or int(time.time())
        spd = fix.get("spd", 0.0) or 0.0

        if lat is None or lon is None:
            return []

        # Count every fix the tracker sees. Used by _update_idle to gate
        # trip-start until the receiver's solution has settled.
        self._fixes_seen += 1

        if self.state == self.STATE_IDLE:
            ev = self._update_idle(lat, lon, ts, spd, fix)
            return [ev] if ev else []
        return self._update_moving(lat, lon, ts, spd, fix)

    # ---- internals ------------------------------------------------------
    def _recap_from_disk(self, trip_id):
        """Read trip's .gps file, compute running totals + peak class."""
        prev_lat = prev_lon = None
        distance_km = 0.0
        max_kmh = 0.0
        all_fixes = []
        idx = 0
        batch = 50
        while True:
            rows = trip_storage.read_fixes_range(trip_id, idx, batch)
            if not rows:
                break
            for r in rows:
                if not r or len(r) < 3:
                    continue
                la, lo = r[1], r[2]
                spd = r[4] if len(r) > 4 and r[4] is not None else 0.0
                if prev_lat is not None:
                    distance_km += approx_distance_m(prev_lat, prev_lon, la, lo) / 1000.0
                if spd > max_kmh:
                    max_kmh = spd
                prev_lat, prev_lon = la, lo
                all_fixes.append(r)
            if len(rows) < batch:
                break
            idx += batch
        peak_class, _peak_avg = classify_by_peak_sustained(all_fixes)
        return {"distance_km": distance_km,
                "max_kmh": max_kmh,
                "peak_class": peak_class}

    def _update_idle(self, lat, lon, ts, spd, fix):
        if self._anchor_lat is None:
            self._anchor_lat = lat
            self._anchor_lon = lon

        moved = approx_distance_m(self._anchor_lat, self._anchor_lon, lat, lon)

        if spd >= MOVE_SPEED_KMH:
            if self._fast_since_ts == 0:
                self._fast_since_ts = ts
        else:
            self._fast_since_ts = 0

        # Motion buffer: hold the most recent fixes that showed
        # non-trivial doppler speed. When a trip-start trigger fires,
        # these become the trip's prefix so start_lat/lon/ts reflect
        # actual departure rather than the trigger moment.
        if spd >= TRIP_LOW_SPEED_KMH:
            self._motion_buffer.append({
                "ts": ts, "lat": lat, "lon": lon,
                "alt": fix.get("alt"), "spd": spd,
                "gps_ts": fix.get("gps_ts"),
            })
            if len(self._motion_buffer) > MOTION_BUFFER_MAX:
                self._motion_buffer.pop(0)
        else:
            # Device went stationary again — discard the buffer; whatever
            # motion was building up didn't pan out as a real trip.
            if self._motion_buffer:
                self._motion_buffer = []

        sustained = (self._fast_since_ts != 0
                     and (ts - self._fast_since_ts) >= MOVE_HOLD_S)
        # `far` must coincide with non-trivial GPS-doppler speed. A
        # stationary device with multipath drift sees moved >> 100m on a
        # single bad fix while spd stays near zero — that signature
        # alone produced ghost trips overnight. Real movement covering
        # 100m always has continuous speed > ~2 km/h.
        far = (moved >= START_DIST_M and spd >= 2.0)

        # GPS-settling gate: refuse to start a trip until the receiver
        # has delivered MIN_FIXES_BEFORE_START accepted fixes since
        # boot. Cold-lock wander produces both phantom distance (`far`)
        # and phantom Doppler speed (`sustained`); only stability waits
        # it out.
        settled = (self._fixes_seen >= MIN_FIXES_BEFORE_START)

        if settled and (sustained or far):
            # Snapshot + clear the buffer before _begin_trip so the
            # trip's prefix is captured atomically; subsequent IDLE
            # state (after a trip ends) starts fresh.
            preceding = self._motion_buffer
            self._motion_buffer = []
            return self._begin_trip(lat, lon, ts, spd, fix, preceding=preceding)

        # Drift anchor toward current position to absorb slow drift
        if self._fast_since_ts == 0:
            self._anchor_lat = self._anchor_lat * 0.9 + lat * 0.1
            self._anchor_lon = self._anchor_lon * 0.9 + lon * 0.1
        return None

    def _begin_trip(self, lat, lon, ts, spd, fix, preceding=None):
        # If we have buffered IDLE fixes that showed motion leading up
        # to this trigger, they belong to the trip — the FIRST one is
        # where the device actually departed, and all of them are part
        # of the trip's polyline.
        preceding = preceding or []
        if preceding:
            first = preceding[0]
            start_ts  = int(first.get("ts") or ts)
            start_lat = first.get("lat", lat)
            start_lon = first.get("lon", lon)
        else:
            start_ts  = ts
            start_lat = lat
            start_lon = lon

        self.state   = self.STATE_MOVING
        self.trip_id = "T{}".format(start_ts)
        self._trip_start_ts  = start_ts
        self._trip_start_lat = start_lat
        self._trip_start_lon = start_lon
        self._last_lat       = start_lat
        self._last_lon       = start_lon
        self._stop_anchor_lat = lat   # trigger-fix coord; current pos
        self._stop_anchor_lon = lon
        self._stop_since_ts  = 0
        self._distance_km    = 0.0
        # Start at 0; the first fix has no "previous" to validate
        # against, so the sustained-max filter requires fix N=2 to
        # also exceed before any value is recorded.
        self._max_speed_kmh  = 0.0
        self._prev_spd       = max(spd, 0.0)
        self._last_fix_gps_ts = (fix.get("gps_ts") or ts)
        self._peak_class    = "walking"
        self._peak_window   = [(ts, spd)]

        msg = {"device": self.device_id,
               "hwid":   self.device_hwid,
               "id":   self.trip_id,
               "ts":   start_ts,
               "lat":  round(start_lat, 6),
               "lon":  round(start_lon, 6)}

        if self._use_storage:
            try:
                trip_storage.open_trip(self.trip_id, {
                    "id":         self.trip_id,
                    "device":     self.device_id,
                    "hwid":       self.device_hwid,
                    "start_ts":   start_ts,
                    "start_lat":  round(start_lat, 6),
                    "start_lon":  round(start_lon, 6),
                    "end_ts":     None,
                })
                # Write the buffered prefix fixes in order, then the
                # trigger fix. Each buffered fix has already passed the
                # GPS module's quality + jump filters, so we trust them.
                for pf in preceding:
                    trip_storage.append_fix({
                        "ts":  int(pf.get("ts")),
                        "lat": pf.get("lat"),
                        "lon": pf.get("lon"),
                        "alt": pf.get("alt"),
                        "spd": pf.get("spd", 0.0),
                    })
                    # Roll distance/max through the prefix so trip
                    # stats include the buffered portion.
                    seg_m = approx_distance_m(
                        self._last_lat, self._last_lon,
                        pf.get("lat"), pf.get("lon"))
                    self._distance_km += seg_m / 1000.0
                    self._last_lat = pf.get("lat")
                    self._last_lon = pf.get("lon")
                    pspd = pf.get("spd", 0.0) or 0.0
                    sustained_max = pspd if pspd < self._prev_spd else self._prev_spd
                    if sustained_max > self._max_speed_kmh:
                        self._max_speed_kmh = sustained_max
                    self._prev_spd = pspd
                # Final: the trigger fix itself (current).
                trip_storage.append_fix({
                    "ts": ts, "lat": lat, "lon": lon,
                    "alt": fix.get("alt"), "spd": spd,
                })
                # Update distance/last for the trigger fix too.
                seg_m = approx_distance_m(self._last_lat, self._last_lon, lat, lon)
                self._distance_km += seg_m / 1000.0
                self._last_lat = lat
                self._last_lon = lon
                sustained_max = spd if spd < self._prev_spd else self._prev_spd
                if sustained_max > self._max_speed_kmh:
                    self._max_speed_kmh = sustained_max
                self._prev_spd = spd
            except Exception:
                pass
        return ("TRIPSTART", msg)

    def _update_moving(self, lat, lon, ts, spd, fix):
        events = []

        # Distance accumulation
        seg_m = approx_distance_m(self._last_lat, self._last_lon, lat, lon)
        self._distance_km += seg_m / 1000.0
        self._last_lat = lat
        self._last_lon = lon

        # Sustained-max filter: take the smaller of the previous and
        # current fix's speed as the "sustained" value. A single-fix
        # spike followed by a normal value won't survive this min.
        sustained = spd if spd < self._prev_spd else self._prev_spd
        if sustained > self._max_speed_kmh:
            self._max_speed_kmh = sustained
        self._prev_spd = spd

        # Track last fix GPS UTC time for the watchdog
        gps_ts = fix.get("gps_ts")
        if gps_ts is not None:
            self._last_fix_gps_ts = gps_ts
        else:
            self._last_fix_gps_ts = ts

        # Persist this fix to disk
        if self._use_storage:
            try:
                trip_storage.append_fix({
                    "ts": ts, "lat": lat, "lon": lon,
                    "alt": fix.get("alt"), "spd": spd,
                })
            except Exception:
                pass

        # ---- update running peak class ----
        # Sliding 5-min window of (ts, spd).  Use MEDIAN (not mean) to be
        # robust against single-fix speed spikes that could otherwise lift
        # a walking trip into cycling class.
        self._peak_window.append((ts, spd))
        cutoff = ts - PEAK_WINDOW_S
        while self._peak_window and self._peak_window[0][0] < cutoff:
            self._peak_window.pop(0)
        if (len(self._peak_window) >= 2
                and (self._peak_window[-1][0] - self._peak_window[0][0]) >= PEAK_WINDOW_S - 30):
            spds = sorted(s for _, s in self._peak_window)
            m = len(spds)
            med = spds[m // 2] if m % 2 == 1 else (spds[m//2-1] + spds[m//2]) / 2.0
            obs_class = classify(med)
            if _class_rank(obs_class) > _class_rank(self._peak_class):
                self._peak_class = obs_class

        # ---- stop detection (the only way trips end now) ----
        from_stop = approx_distance_m(
            self._stop_anchor_lat, self._stop_anchor_lon, lat, lon)
        if from_stop <= END_DIST_M:
            if self._stop_since_ts == 0:
                self._stop_since_ts = ts
            else:
                hold_s = SPLIT_STOP_BY_CLASS.get(self._peak_class, 120)
                if (ts - self._stop_since_ts) >= hold_s:
                    # Trip ends.  end_ts is when the stop began (when we
                    # actually stopped moving), not now (which is hold_s later).
                    ev = self._end_trip(lat, lon, self._stop_since_ts)
                    if ev is not None:   # None = trip was discarded as bogus
                        events.append(ev)
        else:
            self._stop_anchor_lat = lat
            self._stop_anchor_lon = lon
            self._stop_since_ts = 0

        return events

    def _end_trip(self, lat, lon, end_ts, closed_by=None):
        # Final classification: peak-sustained over 5-min window from .gps file.
        # If storage isn't available, fall back to whole-trip avg from RAM.
        msg_type = self._peak_class   # provisional - will be overridden if disk works
        avg_kmh = 0.0
        if self._use_storage and self.trip_id:
            try:
                rows = []
                idx = 0
                batch = 50
                while True:
                    chunk = trip_storage.read_fixes_range(self.trip_id, idx, batch)
                    if not chunk:
                        break
                    rows.extend(chunk)
                    if len(chunk) < batch:
                        break
                    idx += batch
                cls, peak = classify_by_peak_sustained(rows)
                msg_type = cls
                avg_kmh = peak
            except Exception:
                pass
        if avg_kmh == 0.0:
            duration_s = max(1, end_ts - self._trip_start_ts)
            avg_kmh = (self._distance_km / (duration_s / 3600.0)) if duration_s > 0 else 0.0
            msg_type = classify_with_max(avg_kmh, self._max_speed_kmh)

        duration_s = max(1, end_ts - self._trip_start_ts)

        # Confirm the start conditions were real motion. A false-start
        # (e.g. multipath burst that briefly tripped `sustained` or
        # `far` then died) produces a trip with neither distance nor
        # sustained speed. Delete it from flash and don't broadcast
        # TRIPEND — keeps the hub DB and PicoB's sync queue clean.
        confirmed = (
            (self._distance_km * 1000.0) >= MIN_REAL_TRIP_M
            or self._max_speed_kmh        >= MIN_REAL_TRIP_MAX_KMH
        )
        if not confirmed:
            print("[TRIP] discarding unconfirmed trip {} "
                  "(km={:.3f} max={:.2f})".format(
                      self.trip_id, self._distance_km, self._max_speed_kmh))
            if self._use_storage and self.trip_id:
                try:
                    trip_storage.delete_trip(self.trip_id)
                except Exception:
                    pass
            self.state   = self.STATE_IDLE
            self.trip_id = None
            self._anchor_lat = lat
            self._anchor_lon = lon
            self._fast_since_ts = 0
            self._last_fix_gps_ts = None
            return None

        msg = {
            "device": self.device_id,
            "hwid":   self.device_hwid,
            "id":     self.trip_id,
            "sts":    self._trip_start_ts,
            "ets":    end_ts,
            "slat":   round(self._trip_start_lat, 6),
            "slon":   round(self._trip_start_lon, 6),
            "elat":   round(lat, 6),
            "elon":   round(lon, 6),
            "km":     round(self._distance_km, 3),
            "dur":    duration_s,
            "type":   msg_type,
            "avg":    round(avg_kmh, 2),
            "max":    round(self._max_speed_kmh, 2),
            "split":  0,    # historical field; kept for Pi 5 compat
        }
        if closed_by:
            msg["closed_by"] = closed_by
        if self._use_storage:
            try:
                close_meta = {
                    "end_ts":  end_ts,
                    "end_lat": round(lat, 6),
                    "end_lon": round(lon, 6),
                    "type":    msg_type,
                    "km":      round(self._distance_km, 3),
                    "dur":     duration_s,
                    "avg":     round(avg_kmh, 2),
                    "max":     round(self._max_speed_kmh, 2),
                    "split":   0,
                }
                if closed_by:
                    close_meta["closed_by"] = closed_by
                trip_storage.close_trip(close_meta)
            except Exception:
                pass

        # Reset to IDLE
        self.state   = self.STATE_IDLE
        self.trip_id = None
        self._anchor_lat = lat
        self._anchor_lon = lon
        self._fast_since_ts = 0
        self._last_fix_gps_ts = None

        return ("TRIPEND", msg)


# =========================================================================
# Self-tests (CPython, no storage)
# =========================================================================
if __name__ == "__main__":
    print("trip_tracker.py self-test (Round B)")
    print("-" * 50)

    failures = 0
    def mk(): return TripTracker(device_id="T1", use_storage=False)

    # 1. distance + classify (smoke)
    print("\n[1] distance + classify smoke:")
    ok = (abs(approx_distance_m(50.0, 14.0, 50.001, 14.0) - 111) < 5
          and classify(0.5) == "walking"
          and classify(15) == "cycling"
          and classify(60) == "driving")
    print("    {}".format("OK" if ok else "FAIL"))
    if not ok: failures += 1

    # 2. classify_by_peak_sustained: walking-mostly with brief spike -> walking
    print("\n[2] peak-sustained on walk + brief sprint:")
    fixes = []
    for i in range(20):  # 10 min walking 5 km/h, 30s apart
        fixes.append([1000+i*30, 50.0+i*0.0001, 14.0, 200, 5.0])
    fixes.append([1600, 50.002, 14.0, 200, 30.0])  # one fast spike
    for i in range(20):
        fixes.append([1630+i*30, 50.002+i*0.0001, 14.0, 200, 5.0])
    cls, peak = classify_by_peak_sustained(fixes)
    ok = (cls == "walking")
    print("    cls={} peak={:.2f}  expect walking  {}".format(
        cls, peak, "OK" if ok else "FAIL"))
    if not ok: failures += 1

    # 3. peak-sustained: real driving with traffic light -> auto
    print("\n[3] peak-sustained on driving with traffic light:")
    fixes = []
    # 8 min driving 50 km/h
    cur_ts = 1000
    for _ in range(48):
        cur_ts += 10
        fixes.append([cur_ts, 50.0, 14.0, 200, 50.0])
    # 1 min stop
    for _ in range(6):
        cur_ts += 10
        fixes.append([cur_ts, 50.0, 14.0, 200, 0.0])
    cls, peak = classify_by_peak_sustained(fixes)
    ok = (cls == "driving")
    print("    cls={} peak={:.2f}  expect driving  {}".format(
        cls, peak, "OK" if ok else "FAIL"))
    if not ok: failures += 1

    # 4. peak-sustained on short trip (< 5 min) uses whole-trip avg
    print("\n[4] short trip uses whole-trip avg:")
    fixes = []
    for i in range(6):
        fixes.append([1000+i*30, 50.0, 14.0, 200, 18.0])  # 3 min cycling
    cls, peak = classify_by_peak_sustained(fixes)
    ok = (cls == "cycling")
    print("    cls={} peak={:.2f}  expect cycling  {}".format(
        cls, peak, "OK" if ok else "FAIL"))
    if not ok: failures += 1

    # 5. IDLE -> MOVING start
    print("\n[5] start trip on far jump:")
    t = mk()
    t.update({"lat": 50.0, "lon": 14.0, "ts": 1000, "spd": 0.0})
    evs = t.update({"lat": 50.000898, "lon": 14.0, "ts": 1010, "spd": 0.0})
    ok = (len(evs) == 1 and evs[0][0] == "TRIPSTART")
    print("    {}".format("OK" if ok else "FAIL"))
    if not ok: failures += 1

    # 6. Walking trip ends after 120s stationary (at walking peak class)
    print("\n[6] walking trip ends after 120s stop:")
    t = mk()
    cur_ts, lat = 1000, 50.0
    # Start moving slowly
    for _ in range(20):
        cur_ts += 30
        lat += 0.0001
        t.update({"lat": lat, "lon": 14.0, "ts": cur_ts, "spd": 4.0})
    # Stop
    end_ev = None
    for _ in range(10):
        cur_ts += 30
        for e in t.update({"lat": lat, "lon": 14.0, "ts": cur_ts, "spd": 0.0}):
            if e[0] == "TRIPEND": end_ev = e
    ok = (end_ev is not None)
    print("    walking trip ended -> {}".format("OK" if ok else "FAIL"))
    if not ok: failures += 1
    if end_ev:
        ok2 = (end_ev[1].get("type") == "walking")
        print("    type={}  expect walking  {}".format(
            end_ev[1].get("type"), "OK" if ok2 else "FAIL"))
        if not ok2: failures += 1

    # 7. Cycling trip ends after 60s stop
    print("\n[7] cycling trip ends after 60s stop:")
    t = mk()
    cur_ts, lat = 1000, 50.0
    # Cycling fixes for >5 min so peak_class becomes 'cycling'
    for _ in range(30):  # 30 fixes * 10s = 5 min
        cur_ts += 10
        lat += 0.0004
        t.update({"lat": lat, "lon": 14.0, "ts": cur_ts, "spd": 18.0})
    # Stop for 65s
    end_ev = None
    for _ in range(8):  # 80s of stationary fixes
        cur_ts += 10
        for e in t.update({"lat": lat, "lon": 14.0, "ts": cur_ts, "spd": 0.0}):
            if e[0] == "TRIPEND": end_ev = e
    ok = (end_ev is not None)
    print("    cycling trip ended -> {}".format("OK" if ok else "FAIL"))
    if not ok: failures += 1

    # 8. Auto trip survives 60-sec stop (red light), ends at 300s
    print("\n[8] auto trip survives short stop, ends at 300s:")
    t = mk()
    cur_ts, lat = 1000, 50.0
    # 6 min auto
    for _ in range(36):  # 36 * 10s = 6 min
        cur_ts += 10
        lat += 0.001
        t.update({"lat": lat, "lon": 14.0, "ts": cur_ts, "spd": 50.0})
    # 60-sec stop (red light)
    early_evs = []
    for _ in range(7):  # 70s
        cur_ts += 10
        early_evs.extend(t.update({"lat": lat, "lon": 14.0, "ts": cur_ts, "spd": 0.0}))
    ok = (not any(e[0] == "TRIPEND" for e in early_evs))
    print("    no end at 70s stop -> {}".format("OK" if ok else "FAIL: {}".format(early_evs)))
    if not ok: failures += 1
    # Resume driving 1 min
    for _ in range(6):
        cur_ts += 10
        lat += 0.001
        t.update({"lat": lat, "lon": 14.0, "ts": cur_ts, "spd": 50.0})
    # Stop for 320s
    end_ev = None
    for _ in range(33):
        cur_ts += 10
        for e in t.update({"lat": lat, "lon": 14.0, "ts": cur_ts, "spd": 0.0}):
            if e[0] == "TRIPEND": end_ev = e
    ok = (end_ev is not None)
    print("    driving trip ended after 300s -> {}".format("OK" if ok else "FAIL"))
    if not ok: failures += 1
    if end_ev:
        ok2 = (end_ev[1].get("type") == "driving")
        print("    type={}  expect driving  {}".format(
            end_ev[1].get("type"), "OK" if ok2 else "FAIL"))
        if not ok2: failures += 1

    # 9. Walk -> stop -> drive: should produce 2 trips
    print("\n[9] walk -> stop -> drive yields 2 trips:")
    t = mk()
    cur_ts, lat = 1000, 50.0
    starts = ends = 0
    # Walking 6 min
    for _ in range(24):  # 24*15s = 6 min
        cur_ts += 15
        lat += 0.00021  # ~5km/h walking
        for e in t.update({"lat": lat, "lon": 14.0, "ts": cur_ts, "spd": 5.0}):
            if e[0] == "TRIPSTART": starts += 1
            elif e[0] == "TRIPEND": ends += 1
    # Stop 130s (just over 120s walking threshold)
    for _ in range(13):  # 130s
        cur_ts += 10
        for e in t.update({"lat": lat, "lon": 14.0, "ts": cur_ts, "spd": 0.0}):
            if e[0] == "TRIPSTART": starts += 1
            elif e[0] == "TRIPEND": ends += 1
    # Now drive 6 min
    for _ in range(36):
        cur_ts += 10
        lat += 0.001
        for e in t.update({"lat": lat, "lon": 14.0, "ts": cur_ts, "spd": 50.0}):
            if e[0] == "TRIPSTART": starts += 1
            elif e[0] == "TRIPEND": ends += 1
    # End that drive trip with a 320s stop
    for _ in range(33):
        cur_ts += 10
        for e in t.update({"lat": lat, "lon": 14.0, "ts": cur_ts, "spd": 0.0}):
            if e[0] == "TRIPSTART": starts += 1
            elif e[0] == "TRIPEND": ends += 1
    ok = (starts == 2 and ends == 2)
    print("    starts={} ends={}  (expect 2 each)  {}".format(
        starts, ends, "OK" if ok else "FAIL"))
    if not ok: failures += 1

    # 10. force_close (watchdog) ends an open trip
    print("\n[10] force_close ends an active trip:")
    t = mk()
    cur_ts, lat = 1000, 50.0
    # Start a walking trip and feed a few fixes
    for _ in range(4):
        cur_ts += 15
        lat += 0.0002
        t.update({"lat": lat, "lon": 14.0, "ts": cur_ts, "spd": 4.0,
                  "gps_ts": cur_ts})
    ok = (t.in_trip() is True)
    print("    in_trip after start: {}".format("OK" if ok else "FAIL"))
    if not ok: failures += 1
    # Force close (simulating "GPS lost for 5+ min" watchdog)
    ev = t.force_close(reason="watchdog_no_fix")
    ok = (ev is not None and ev[0] == "TRIPEND"
          and ev[1].get("closed_by") == "watchdog_no_fix"
          and not t.in_trip())
    print("    force_close result: kind={} closed_by={} in_trip={}  {}".format(
        ev[0] if ev else None,
        ev[1].get("closed_by") if ev else None,
        t.in_trip(),
        "OK" if ok else "FAIL"))
    if not ok: failures += 1

    # 11. force_close on idle returns None
    print("\n[11] force_close while IDLE returns None:")
    t = mk()
    ev = t.force_close()
    ok = (ev is None)
    print("    {}".format("OK" if ok else "FAIL"))
    if not ok: failures += 1

    print()
    print("ALL SELF-TESTS PASSED" if failures == 0 else "{} FAILURES".format(failures))

    # ---- classify_with_max tests ----
    print("\n--- classify_with_max ---")
    cases = [
        # (avg, max, expected, description)
        (20.2, 45.6, "driving", "car in traffic: avg=20 max=45"),
        (12.0, 55.0, "driving", "stop-start city driving: avg=12 max=55"),
        (22.0, 35.0, "cycling", "fast cyclist: avg=22 max=35"),
        (5.0,  8.0,  "walking", "walking: avg=5 max=8"),
        (24.0, 32.0, "driving", "avg=24 max=32 -> driving"),
        (18.0, 32.0, "cycling", "max=32 but avg<24 -> cycling"),
        (3.0,  6.0,  "walking", "slow walk"),
        (60.0, 94.9, "driving", "highway: avg=60 max=95"),
        (21.9, 39.9, "cycling", "just below both thresholds -> cycling"),
        (18.0, 40.0, "driving", "max=40 hard boundary -> driving"),
    ]
    for avg, mx, expected, desc in cases:
        got = classify_with_max(avg, mx)
        ok = got == expected
        print("  avg={:5.1f} max={:5.1f} -> {:8s} expect {:8s}  {}  {}".format(
            avg, mx, got, expected, "OK" if ok else "FAIL", desc))
        if not ok: failures += 1
    print("classify_with_max: {} failures".format(failures))