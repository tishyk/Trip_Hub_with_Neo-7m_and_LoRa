"""
gps_module.py - Non-blocking NMEA GPS reader for Pico B.

Reads $GPGGA and $GPRMC sentences from UART0 (default GPIO 0/1, 9600 baud)
and exposes the latest fix as a dict. Designed to be polled inside
runtime.py's main loop alongside chat input - never blocks.

Usage:
    from gps_module import Gps
    gps = Gps(send_interval_s=30)        # opens UART, ready to read
    while True:
        fix = gps.poll()                  # returns dict on schedule, else None
        if fix is not None:
            print("send:", fix)           # caller TXes via LoRa

A fix dict looks like:
    {"lat": 50.0755, "lon": 14.4378, "alt": 205.3, "spd": 4.2, "ts": 1730290015}

Fields:
    lat  - decimal degrees, +N / -S
    lon  - decimal degrees, +E / -W
    alt  - meters above sea level (omitted if not available)
    spd  - speed over ground in km/h (omitted if not available)
    ts   - unix timestamp from local clock (NEO-7M time-of-day not used)

Behavior:
    - First poll() drains UART; we read up to ~32 lines per call so we never
      sit too long in one place
    - Only returns a fix when:
        (a) we have at least one valid GGA or RMC parse
        (b) at least send_interval_s has elapsed since last returned fix
    - If no fix yet (cold start, indoors), poll() returns None forever
    - Prints a one-time "GPS: first fix" line to Thonny when fix acquired
"""

import time
try:
    from machine import UART, Pin
    _HAS_HW = True
except ImportError:
    # Running on CPython for self-test - stub out machine
    _HAS_HW = False


# ---- defaults ----
GPS_UART_ID  = 0
GPS_BAUD     = 9600
GPS_TX_PIN   = 0   # Pico GPIO 0  (Pico's TX, into GPS RX)
GPS_RX_PIN   = 1   # Pico GPIO 1  (Pico's RX, from GPS TX)


# =============================================================================
# NMEA parsing helpers (pure functions, easy to self-test)
# =============================================================================

def _nmea_to_decimal(token, hemisphere):
    """Convert NMEA 'ddmm.mmmm' or 'dddmm.mmmm' + hemisphere -> decimal degrees.

    Returns float or None on bad input.
    """
    if not token or not hemisphere:
        return None
    try:
        # Find the dot - degrees are everything before (mm.mmmm part)
        dot = token.find('.')
        if dot < 3:
            return None
        # Last 2 digits before the dot are MM, rest are degrees
        deg = int(token[:dot - 2])
        minutes = float(token[dot - 2:])
    except (ValueError, IndexError):
        return None
    val = deg + minutes / 60.0
    if hemisphere in ('S', 'W'):
        val = -val
    return val


def _checksum_ok(line):
    """Verify NMEA checksum. line is the full sentence including $ and *XX."""
    star = line.rfind('*')
    if star < 1 or star + 3 > len(line):
        return False
    body = line[1:star]   # everything between $ and *
    try:
        expected = int(line[star + 1:star + 3], 16)
    except ValueError:
        return False
    chk = 0
    for ch in body:
        chk ^= ord(ch)
    return chk == expected


# ---- jump-filter constants ----
# These tune the post-poll() position-jump rejection.  See poll() docstring.
MIN_DELTA_FLOOR_M       = 10.0   # threshold floor when median-delta is small
DELTA_NOISE_M           = 20.0   # GPS noise tolerance added to threshold
DELTA_HISTORY_LEN       = 5      # number of recent deltas to median over
MAX_CONSECUTIVE_REJECTS = 3      # force-accept after this many rejections

# ---- per-fix quality gate ----
# Rejects fixes with bad satellite geometry (the receiver itself is
# warning us). Classic 'low sats / high HDOP' moments are when GPS
# multipath spikes happen — drop them at source so neither the trip
# tracker nor the live stream ever sees them.
#
# Thresholds chosen for marginal-sky-view use (urban, near windows,
# light forest). HDOP 10 implies ~25 m position uncertainty, which
# at walking cadence (1 fix per 15 s, ~20 m of real motion) still
# produces a recognisable polyline. The position-jump filter in
# poll() catches multi-fix glitches even when HDOP is moderate.
MIN_NSAT = 4      # 4 sats minimum for any 3D fix
MAX_HDOP = 10.0   # 'moderate' geometry is acceptable for walking


try:
    import math as _math_for_dist
    _HAS_MATH = True
except Exception:
    _HAS_MATH = False


def _approx_distance_m(lat1, lon1, lat2, lon2):
    """Equirectangular approximation, meters.  Used by the jump filter."""
    avg_lat_rad = (lat1 + lat2) * 0.5 * 0.0174532925
    if _HAS_MATH:
        cos_lat = _math_for_dist.cos(avg_lat_rad)
    else:
        cos_lat = 1.0
    dlat = (lat2 - lat1) * 111320.0
    dlon = (lon2 - lon1) * 111320.0 * cos_lat
    if _HAS_MATH:
        return _math_for_dist.sqrt(dlat * dlat + dlon * dlon)
    return abs(dlat) + abs(dlon)


def parse_gpgga(line):
    """Parse a $GPGGA sentence -> {"lat","lon","alt","fix","nsat","hdop"} or None.

    fix:  integer 0=no fix, 1=GPS, 2=DGPS.
    nsat: number of satellites in use (int).
    hdop: horizontal dilution of precision (float).

    Returns None if line is malformed.
    """
    if not line.startswith('$GPGGA') and not line.startswith('$GNGGA'):
        return None
    if not _checksum_ok(line):
        return None
    parts = line.split(',')
    # GGA fields: 0=$GPGGA, 1=time, 2=lat, 3=N/S, 4=lon, 5=E/W,
    # 6=fix quality, 7=#sats, 8=hdop, 9=alt, 10=alt unit
    if len(parts) < 11:
        return None
    try:
        fix = int(parts[6]) if parts[6] else 0
    except ValueError:
        fix = 0
    if fix == 0:
        return {'fix': 0}
    lat = _nmea_to_decimal(parts[2], parts[3])
    lon = _nmea_to_decimal(parts[4], parts[5])
    if lat is None or lon is None:
        return None
    out = {'fix': fix, 'lat': lat, 'lon': lon}
    try:
        if parts[7]:
            out['nsat'] = int(parts[7])
    except ValueError:
        pass
    try:
        if parts[8]:
            out['hdop'] = float(parts[8])
    except ValueError:
        pass
    try:
        if parts[9]:
            out['alt'] = float(parts[9])
    except ValueError:
        pass
    return out


def _rmc_to_unix(time_str, date_str):
    """Combine RMC time (HHMMSS or HHMMSS.sss) + date (DDMMYY) into unix epoch.
    Returns int seconds, or None on failure.

    Pure integer math so we don't depend on the time/datetime module's
    ability to handle arbitrary years on MicroPython (its epoch base is 2000
    on RP2040, not 1970).  We compute days-from-epoch manually.
    """
    if not time_str or not date_str:
        return None
    try:
        # time HHMMSS(.sss)
        if '.' in time_str:
            time_str = time_str.split('.', 1)[0]
        if len(time_str) < 6:
            return None
        hh = int(time_str[0:2])
        mm = int(time_str[2:4])
        ss = int(time_str[4:6])
        # date DDMMYY (year is 2-digit, assume 20xx)
        if len(date_str) != 6:
            return None
        dd = int(date_str[0:2])
        mo = int(date_str[2:4])
        yy = int(date_str[4:6])
        year = 2000 + yy
    except ValueError:
        return None

    # Days since 1970-01-01 (UTC).  Cumulative days at start of each month
    # for non-leap years.
    cumdays = (0, 31, 59, 90, 120, 151, 181, 212, 243, 273, 304, 334)
    # Years 1970..year-1 contribute their day counts
    days = 0
    for y in range(1970, year):
        days += 366 if _is_leap(y) else 365
    days += cumdays[mo - 1]
    # Add leap-day for current year if past Feb in a leap year
    if mo > 2 and _is_leap(year):
        days += 1
    days += dd - 1
    return days * 86400 + hh * 3600 + mm * 60 + ss


def _is_leap(y):
    return (y % 4 == 0 and y % 100 != 0) or (y % 400 == 0)


def parse_gprmc(line):
    """Parse a $GPRMC sentence -> {"lat","lon","spd","valid","ts"} or None.

    valid: True if 'A' (active), False if 'V' (void).
    spd is converted from knots to km/h.
    ts: unix epoch seconds from RMC's UTC time+date (when valid + parseable).
    """
    if not line.startswith('$GPRMC') and not line.startswith('$GNRMC'):
        return None
    if not _checksum_ok(line):
        return None
    parts = line.split(',')
    # RMC fields: 0=$GPRMC, 1=time, 2=A/V, 3=lat, 4=N/S, 5=lon, 6=E/W,
    # 7=speed knots, 8=track, 9=date, 10=mag var, 11=E/W, 12=mode*chk
    if len(parts) < 10:
        return None
    valid = (parts[2] == 'A')
    if not valid:
        return {'valid': False}
    lat = _nmea_to_decimal(parts[3], parts[4])
    lon = _nmea_to_decimal(parts[5], parts[6])
    if lat is None or lon is None:
        return None
    out = {'valid': True, 'lat': lat, 'lon': lon}
    try:
        if parts[7]:
            knots = float(parts[7])
            out['spd'] = knots * 1.852  # knots -> km/h
    except ValueError:
        pass
    # GPS UTC time (preferred over Pico clock)
    ts = _rmc_to_unix(parts[1], parts[9])
    if ts is not None:
        out['ts'] = ts
    return out


# =============================================================================
# Gps class - reads UART, holds latest fix, throttles output
# =============================================================================

class Gps:
    def __init__(self, send_interval_s=30,
                 uart_id=GPS_UART_ID, baud=GPS_BAUD,
                 tx_pin=GPS_TX_PIN, rx_pin=GPS_RX_PIN,
                 max_lines_per_poll=32):
        self.send_interval_s = send_interval_s
        self.max_lines_per_poll = max_lines_per_poll

        # State
        self._latest = None         # dict with lat/lon/(alt)/(spd) or None
        self._has_fix = False
        self._announced_fix = False
        self._last_send_at = 0      # epoch seconds, 0 = never
        self._line_buf = b""        # for partial-line accumulation

        # Filter state (for dedup + jump rejection)
        self._last_returned_gps_ts = None  # gps_ts of the most recent fix we
                                           # actually returned to caller
        self._last_returned_lat    = None
        self._last_returned_lon    = None
        self._recent_deltas        = []    # last few accepted-delta values (m)
        self._consecutive_rejects  = 0     # for force-accept after persistent rejection

        if _HAS_HW:
            self.uart = UART(uart_id, baudrate=baud, bits=8,
                             parity=None, stop=1)
            self.uart.init(tx=Pin(tx_pin), rx=Pin(rx_pin),
                           baudrate=baud)
        else:
            self.uart = None  # CPython stub

    # ---- internal: feed bytes from UART into parsers ------------------
    def _ingest_line(self, line):
        """Take one decoded NMEA line, update self._latest if it's useful."""
        if not line:
            return
        gga = parse_gpgga(line)
        if gga is not None:
            if gga.get('fix', 0) > 0:
                # Quality gate — receiver-flagged bad geometry should
                # not become a "valid" fix. Multipath spikes happen
                # almost exclusively in these moments.
                nsat = gga.get('nsat')
                hdop = gga.get('hdop')
                if (nsat is not None and nsat < MIN_NSAT) or \
                   (hdop is not None and hdop > MAX_HDOP):
                    # Stash quality on _latest so RMC merging can also
                    # consult it, then reject this GGA's coords. Init
                    # _latest with an empty dict if it's None — without
                    # this, the first cold-lock RMC would sneak through
                    # because no nsat/hdop is on record to fail.
                    if self._latest is None:
                        self._latest = {}
                    if nsat is not None: self._latest['nsat'] = nsat
                    if hdop is not None: self._latest['hdop'] = hdop
                    return
                # Merge into latest fix
                merged = self._latest.copy() if self._latest else {}
                merged['lat'] = gga['lat']
                merged['lon'] = gga['lon']
                if nsat is not None: merged['nsat'] = nsat
                if hdop is not None: merged['hdop'] = hdop
                if 'alt' in gga:
                    merged['alt'] = gga['alt']
                self._latest = merged
                self._has_fix = True
                if not self._announced_fix:
                    self._announced_fix = True
                    print("GPS: first fix acquired (lat=%.5f lon=%.5f)" % (
                        gga['lat'], gga['lon']))
            return
        rmc = parse_gprmc(line)
        if rmc is not None:
            if rmc.get('valid'):
                # Honour the most recent GGA's quality gate when one
                # has been seen. First-fix case (no GGA yet) is allowed
                # so we don't deadlock on cold start.
                cur_nsat = self._latest.get('nsat') if self._latest else None
                cur_hdop = self._latest.get('hdop') if self._latest else None
                if (cur_nsat is not None and cur_nsat < MIN_NSAT) or \
                   (cur_hdop is not None and cur_hdop > MAX_HDOP):
                    return
                merged = self._latest.copy() if self._latest else {}
                merged['lat'] = rmc['lat']
                merged['lon'] = rmc['lon']
                if 'spd' in rmc:
                    merged['spd'] = rmc['spd']
                if 'ts' in rmc:
                    # GPS UTC time - much more reliable than Pico clock.
                    merged['gps_ts'] = rmc['ts']
                self._latest = merged
                self._has_fix = True
                if not self._announced_fix:
                    self._announced_fix = True
                    print("GPS: first fix acquired (lat=%.5f lon=%.5f)" % (
                        rmc['lat'], rmc['lon']))

    def _drain_uart(self):
        """Pull whatever is available. Bounded by max_lines_per_poll."""
        if self.uart is None:
            return
        lines_read = 0
        while lines_read < self.max_lines_per_poll:
            try:
                if not self.uart.any():
                    return
                chunk = self.uart.read(64)
            except Exception:
                return
            if not chunk:
                return
            self._line_buf += chunk
            # Split on newline; keep the trailing partial in the buffer
            while b'\n' in self._line_buf:
                line_b, self._line_buf = self._line_buf.split(b'\n', 1)
                lines_read += 1
                try:
                    line = line_b.decode('utf-8').strip()
                except Exception:
                    continue
                if line:
                    self._ingest_line(line)
            # Cap buffer so a no-newline stream doesn't grow forever
            if len(self._line_buf) > 512:
                self._line_buf = b""

    # ---- public API ---------------------------------------------------
    def has_fix(self):
        return self._has_fix

    def latest(self):
        """Return the latest fix dict (without modifying send schedule), or None."""
        return self._latest

    def set_interval(self, send_interval_s):
        """Adjust the send interval at runtime.  Used by runtime.py to switch
        between IDLE (60s) and MOVING (30s) cadences."""
        if send_interval_s and send_interval_s > 0:
            self.send_interval_s = send_interval_s

    def poll(self):
        """Drain UART. Maybe return a fresh fix.

        Filtering rules (in order):
            1. THROTTLE: only consider returning a fix if at least
               send_interval_s have elapsed since the last accepted fix
               (Pico clock).  This is the cadence control.
            2. DEDUP: if the cached _latest has the same gps_ts as the
               last fix we returned, return None.  Prevents duplicate
               fixes from being saved when GPS hasn't sent a new RMC.
            3. JUMP REJECTION: if the new position is implausibly far
               from the last returned position given the time elapsed
               (multipath / GPS error spike), return None.  After
               MAX_CONSECUTIVE_REJECTS in a row, force-accept the next
               fix to handle genuine sudden acceleration.

        On rejection (steps 2-3), the throttle is NOT advanced, so the
        next loop iteration can immediately re-poll with no extra delay
        beyond the natural 1 Hz GPS sentence rate.

        Returned fix dict has 'ts' set to GPS UTC time when available,
        else Pico clock as fallback.  'gps_ts' if present signals "this
        came from GPS".
        """
        self._drain_uart()
        if not self._has_fix or self._latest is None:
            return None
        now = time.time()

        # 1. Throttle (cadence control)
        if not (self._last_send_at == 0
                or (now - self._last_send_at) >= self.send_interval_s):
            return None

        # 2. Dedup against last returned gps_ts
        cur_gps_ts = self._latest.get('gps_ts')
        if cur_gps_ts is not None and cur_gps_ts == self._last_returned_gps_ts:
            return None

        # 3. Jump rejection - only when we have a previous accepted fix
        cur_lat = self._latest.get('lat')
        cur_lon = self._latest.get('lon')
        if (cur_lat is not None and cur_lon is not None
                and self._last_returned_lat is not None):
            delta_m = _approx_distance_m(
                self._last_returned_lat, self._last_returned_lon,
                cur_lat, cur_lon)
            if self._recent_deltas:
                sorted_d = sorted(self._recent_deltas)
                median_d = sorted_d[len(sorted_d) // 2]
            else:
                median_d = 0.0
            threshold = max(2.0 * median_d, MIN_DELTA_FLOOR_M) + DELTA_NOISE_M

            if (delta_m > threshold
                    and self._consecutive_rejects < MAX_CONSECUTIVE_REJECTS):
                self._consecutive_rejects += 1
                # Don't advance throttle - allow immediate retry next iteration
                return None
            # Either OK or we hit max rejects -> force-accept and reset state
            self._consecutive_rejects = 0
            self._recent_deltas.append(delta_m)
            if len(self._recent_deltas) > DELTA_HISTORY_LEN:
                self._recent_deltas.pop(0)
        # First fix: accept unconditionally

        # Accept - update returned-state and throttle
        self._last_send_at = now
        self._last_returned_gps_ts = cur_gps_ts
        self._last_returned_lat    = cur_lat
        self._last_returned_lon    = cur_lon

        out = self._latest.copy()
        if cur_gps_ts is not None:
            out['ts'] = int(cur_gps_ts)
        else:
            out['ts'] = int(now)
        return out

    def latest_gps_ts(self):
        """Return the most-recently-seen GPS UTC ts, or None if no RMC fix yet.
        Used by trip_storage to decide if a stale trip should be auto-closed."""
        if self._latest is None:
            return None
        return self._latest.get('gps_ts')


# =============================================================================
# Self-test - runs on CPython without hardware
# =============================================================================
if __name__ == "__main__":
    print("gps_module.py self-test")
    print("-" * 50)

    failures = 0

    # --- 1. NMEA-to-decimal conversion ---
    print("\n[1] _nmea_to_decimal:")
    cases = [
        ("5004.5300", "N",  50.0755),    # 50 deg + 4.53/60 min
        ("01426.2680", "E", 14.43780),   # 14 deg + 26.268/60 min
        ("5004.5300", "S", -50.0755),
        ("01426.2680", "W", -14.43780),
        ("",          "N",  None),
        ("4.5",       "N",  None),       # no degrees part
    ]
    for token, hemi, expected in cases:
        got = _nmea_to_decimal(token, hemi)
        if expected is None:
            ok = (got is None)
        else:
            ok = (got is not None and abs(got - expected) < 1e-4)
        print("    {!r:14s} {} -> {}  ({})".format(
            token, hemi, got, "OK" if ok else "FAIL expected " + str(expected)))
        if not ok:
            failures += 1

    # --- 2. Checksum ---
    print("\n[2] _checksum_ok:")
    # Real GGA sentence (Prague)
    valid = "$GPGGA,123519,5004.5300,N,01426.2680,E,1,08,0.9,205.3,M,46.9,M,,*5C"
    invalid = "$GPGGA,123519,5004.5300,N,01426.2680,E,1,08,0.9,205.3,M,46.9,M,,*FF"
    # Compute the actual checksum so the test is robust
    body = valid[1:valid.rfind('*')]
    real_chk = 0
    for c in body: real_chk ^= ord(c)
    valid_real = valid[:valid.rfind('*')+1] + "{:02X}".format(real_chk)
    print("    valid sentence  -> {}".format(_checksum_ok(valid_real)))
    print("    bad checksum    -> {}".format(_checksum_ok(invalid)))
    if not _checksum_ok(valid_real):  failures += 1
    if _checksum_ok(invalid):         failures += 1

    # --- 3. parse_gpgga: with fix ---
    print("\n[3] parse_gpgga with valid fix:")
    r = parse_gpgga(valid_real)
    ok = (r is not None and r.get('fix') == 1
          and abs(r['lat'] - 50.0755) < 1e-3
          and abs(r['lon'] - 14.4378) < 1e-3
          and abs(r['alt'] - 205.3)   < 1e-1)
    print("    parsed: {}".format(r))
    print("    -> {}".format("OK" if ok else "FAIL"))
    if not ok: failures += 1

    # --- 4. parse_gpgga: no fix ---
    print("\n[4] parse_gpgga with no fix:")
    no_fix_body = "GPGGA,123519,,,,,0,00,,,M,,M,,"
    chk = 0
    for c in no_fix_body: chk ^= ord(c)
    no_fix = "$" + no_fix_body + "*{:02X}".format(chk)
    r = parse_gpgga(no_fix)
    ok = (r is not None and r.get('fix') == 0 and 'lat' not in r)
    print("    parsed: {}".format(r))
    print("    -> {}".format("OK" if ok else "FAIL"))
    if not ok: failures += 1

    # --- 5. parse_gprmc: valid 'A' ---
    print("\n[5] parse_gprmc with valid (A):")
    rmc_body = "GPRMC,123519,A,5004.5300,N,01426.2680,E,5.0,054.7,011024,,"
    chk = 0
    for c in rmc_body: chk ^= ord(c)
    rmc_line = "$" + rmc_body + "*{:02X}".format(chk)
    r = parse_gprmc(rmc_line)
    ok = (r is not None and r.get('valid') is True
          and abs(r['lat'] - 50.0755) < 1e-3
          and abs(r['lon'] - 14.4378) < 1e-3
          and abs(r['spd'] - 5.0 * 1.852) < 0.01)
    print("    parsed: {}".format(r))
    print("    -> {}".format("OK" if ok else "FAIL"))
    if not ok: failures += 1

    # --- 6. parse_gprmc: void 'V' ---
    print("\n[6] parse_gprmc with void (V):")
    void_body = "GPRMC,123519,V,,,,,,,011024,,"
    chk = 0
    for c in void_body: chk ^= ord(c)
    void_line = "$" + void_body + "*{:02X}".format(chk)
    r = parse_gprmc(void_line)
    ok = (r is not None and r.get('valid') is False)
    print("    parsed: {}".format(r))
    print("    -> {}".format("OK" if ok else "FAIL"))
    if not ok: failures += 1

    # --- 7. Wrong sentence types ---
    print("\n[7] non-GGA/RMC sentences ignored:")
    bogus = ["$GPGSV,3,1,12,*7B", "junk", "", "$GPVTG,xxx*00"]
    ok = all(parse_gpgga(b) is None and parse_gprmc(b) is None for b in bogus)
    print("    -> {}".format("OK" if ok else "FAIL"))
    if not ok: failures += 1

    # --- 8. Gps class throttling ---
    print("\n[8] Gps throttling (no hardware):")
    g = Gps(send_interval_s=2)
    # Inject a fix manually
    g._latest = {'lat': 50.0755, 'lon': 14.4378, 'alt': 205.3, 'spd': 9.26}
    g._has_fix = True
    first = g.poll()
    second = g.poll()  # immediate, should be None (throttled)
    ok1 = (first is not None and first['lat'] == 50.0755
           and 'ts' in first)
    ok2 = (second is None)
    print("    first poll returns fix    -> {}".format("OK" if ok1 else "FAIL"))
    print("    second poll throttled None -> {}".format("OK" if ok2 else "FAIL"))
    if not ok1: failures += 1
    if not ok2: failures += 1

    # --- 9. _ingest_line happy path ---
    print("\n[9] _ingest_line announces first fix:")
    g2 = Gps(send_interval_s=30)
    g2._ingest_line(valid_real)
    ok = (g2.has_fix() and g2._latest is not None
          and abs(g2._latest['lat'] - 50.0755) < 1e-3)
    print("    has_fix() after ingest    -> {}".format("OK" if ok else "FAIL"))
    if not ok: failures += 1

    # --- 10. _rmc_to_unix ---
    print("\n[10] _rmc_to_unix:")
    cases10 = [
        # 2024-10-11 12:35:19 UTC  -> 1728650119
        ("123519", "111024", 1728650119),
        # 2021-01-01 00:00:00 UTC  -> 1609459200
        ("000000", "010121", 1609459200),
        # 2020-02-29 (leap day)    -> 1582934400
        ("000000", "290220", 1582934400),
        # 2024-03-01 (after leap)  -> 1709251200
        ("000000", "010324", 1709251200),
        # invalid inputs
        ("",       "111024", None),
        ("123519", "",       None),
        ("xx5519", "111024", None),
    ]
    for t_str, d_str, expected in cases10:
        got = _rmc_to_unix(t_str, d_str)
        ok = (got == expected)
        print("    time={!r:8s} date={!r:8s} -> {!s:12s}  expect {}  {}".format(
            t_str, d_str, str(got), expected, "OK" if ok else "FAIL"))
        if not ok: failures += 1

    # --- 11. parse_gprmc returns ts ---
    print("\n[11] parse_gprmc carries UTC ts:")
    rmc_body = "GPRMC,123519,A,5004.5300,N,01426.2680,E,5.0,054.7,111024,,"
    chk = 0
    for c in rmc_body: chk ^= ord(c)
    rmc_line = "$" + rmc_body + "*{:02X}".format(chk)
    r = parse_gprmc(rmc_line)
    ok = (r is not None and r.get('valid') and r.get('ts') == 1728650119)
    print("    parsed ts={} expect 1728650119  {}".format(
        r.get('ts') if r else None, "OK" if ok else "FAIL"))
    if not ok: failures += 1

    # --- 12. poll uses GPS ts when available ---
    print("\n[12] poll prefers GPS ts over Pico clock:")
    g3 = Gps(send_interval_s=30)
    g3._latest = {"lat": 50.0, "lon": 14.0, "spd": 1.0, "gps_ts": 1728650119}
    g3._has_fix = True
    fix = g3.poll()
    ok = (fix is not None and fix['ts'] == 1728650119)
    print("    fix ts={} expect 1728650119  {}".format(
        fix.get('ts') if fix else None, "OK" if ok else "FAIL"))
    if not ok: failures += 1

    # --- 13. Same gps_ts returned twice -> second is None (dedup) ---
    print("\n[13] dedup: second poll with same gps_ts returns None:")
    g4 = Gps(send_interval_s=0)   # no throttle, focus on dedup
    g4._latest = {"lat": 50.0, "lon": 14.0, "spd": 1.0, "gps_ts": 100}
    g4._has_fix = True
    f1 = g4.poll()
    f2 = g4.poll()
    ok = (f1 is not None and f2 is None)
    print("    1st={}  2nd={}  {}".format(
        "fix" if f1 else "None",
        "fix" if f2 else "None",
        "OK" if ok else "FAIL"))
    if not ok: failures += 1

    # --- 14. New gps_ts allows return ---
    print("\n[14] new gps_ts allows another return:")
    g4._latest = {"lat": 50.0, "lon": 14.0, "spd": 1.0, "gps_ts": 101}
    f3 = g4.poll()
    ok = (f3 is not None)
    print("    new gps_ts -> {}  {}".format("returned" if f3 else "None",
                                              "OK" if ok else "FAIL"))
    if not ok: failures += 1

    # --- 15. Implausible jump rejected (after building median delta) ---
    print("\n[15] jump filter rejects implausible delta:")
    g5 = Gps(send_interval_s=0)
    g5._has_fix = True
    # Feed 5 walking-paced fixes (~20m each)
    base_lat = 50.0
    for i in range(5):
        g5._latest = {"lat": base_lat + i*0.00018,  # ~20m per step
                      "lon": 14.0, "spd": 5.0, "gps_ts": 1000+i}
        g5.poll()
    # Now feed a 200m jump
    g5._latest = {"lat": base_lat + 5*0.00018 + 0.0018,  # +200m
                  "lon": 14.0, "spd": 5.0, "gps_ts": 1006}
    f = g5.poll()
    ok = (f is None and g5._consecutive_rejects == 1)
    print("    big jump -> rejected={} consec_rej={}  {}".format(
        f is None, g5._consecutive_rejects, "OK" if ok else "FAIL"))
    if not ok: failures += 1

    # --- 16. After 3 rejects, 4th big jump force-accepted ---
    print("\n[16] force-accept after 3 consecutive rejects:")
    # State already has 1 reject from test 15. Feed two more rejects.
    g5._latest = {"lat": base_lat + 5*0.00018 + 0.002,
                  "lon": 14.0, "spd": 5.0, "gps_ts": 1007}
    f = g5.poll()  # 2nd reject
    g5._latest = {"lat": base_lat + 5*0.00018 + 0.0022,
                  "lon": 14.0, "spd": 5.0, "gps_ts": 1008}
    f = g5.poll()  # 3rd reject
    g5._latest = {"lat": base_lat + 5*0.00018 + 0.0024,
                  "lon": 14.0, "spd": 5.0, "gps_ts": 1009}
    f = g5.poll()  # should force-accept now
    ok = (f is not None and g5._consecutive_rejects == 0)
    print("    after 3 rejects -> 4th accepted={} consec_rej={}  {}".format(
        f is not None, g5._consecutive_rejects, "OK" if ok else "FAIL"))
    if not ok: failures += 1

    # --- 17. First fix accepted unconditionally (empty history) ---
    print("\n[17] first fix accepted unconditionally:")
    g6 = Gps(send_interval_s=0)
    g6._has_fix = True
    g6._latest = {"lat": 50.0, "lon": 14.0, "spd": 1.0, "gps_ts": 1}
    f = g6.poll()
    ok = (f is not None)
    print("    {}  {}".format("accepted" if f else "rejected",
                                "OK" if ok else "FAIL"))
    if not ok: failures += 1

    print()
    print("Logic tests: {} FAILURES".format(failures) if failures
          else "Logic tests: ALL PASSED")

    # =========================================================================
    # Hardware diagnostics - only run on a Pico with the GPS wired up
    # =========================================================================
    if _HAS_HW:
        print()
        print("=" * 50)
        print("HARDWARE DIAGNOSTICS")
        print("=" * 50)

        # ---- 10. UART read - count NMEA lines for 10 seconds ----
        print("\n[10] UART read for 10 seconds (UART0, TX=0, RX=1, 9600):")
        try:
            uart = UART(GPS_UART_ID, baudrate=GPS_BAUD, bits=8,
                        parity=None, stop=1)
            uart.init(tx=Pin(GPS_TX_PIN), rx=Pin(GPS_RX_PIN),
                      baudrate=GPS_BAUD)
        except Exception as e:
            print("    UART open failed: {}".format(e))
            uart = None

        if uart is not None:
            t0 = time.time()
            total_lines = 0
            decode_ok   = 0
            decode_fail = 0
            nmea_lines  = 0
            gga_lines   = 0
            rmc_lines   = 0
            other_lines = 0
            buf = b""
            while time.time() - t0 < 10:
                try:
                    if uart.any():
                        chunk = uart.read(64)
                        if chunk:
                            buf += chunk
                            while b'\n' in buf:
                                line_b, buf = buf.split(b'\n', 1)
                                total_lines += 1
                                try:
                                    line = line_b.decode('utf-8').strip()
                                    decode_ok += 1
                                except Exception:
                                    decode_fail += 1
                                    continue
                                if not line:
                                    continue
                                if line.startswith('$GP') or line.startswith('$GN'):
                                    nmea_lines += 1
                                    if line.startswith('$GPGGA') or line.startswith('$GNGGA'):
                                        gga_lines += 1
                                    elif line.startswith('$GPRMC') or line.startswith('$GNRMC'):
                                        rmc_lines += 1
                                else:
                                    other_lines += 1
                except Exception as e:
                    print("    read error: {}".format(e))
                    break
                time.sleep(0.05)

            print("    Total lines     : {}".format(total_lines))
            print("    Decoded OK      : {}".format(decode_ok))
            print("    Decode failed   : {}".format(decode_fail))
            print("    NMEA ($GP/$GN)  : {}".format(nmea_lines))
            print("    GGA sentences   : {}".format(gga_lines))
            print("    RMC sentences   : {}".format(rmc_lines))
            print("    Non-NMEA lines  : {}".format(other_lines))

            if total_lines == 0:
                print()
                print("    [!] No data received from GPS module.")
                print("        Check wiring:")
                print("          GPS VCC   -> Pico 3V3 or VBUS (per board variant)")
                print("          GPS GND   -> Pico GND")
                print("          GPS TX    -> Pico GPIO 1 (RX)")
                print("          GPS RX    -> Pico GPIO 0 (TX)")
                print("        Most common mistake: TX/RX swapped.")
            elif nmea_lines == 0:
                print()
                print("    [!] Got data but no NMEA sentences.")
                print("        Module may be in binary (UBX) mode or wrong baud.")
            else:
                print()
                print("    OK - GPS module is sending NMEA data.")

        # ---- 11. Wait up to 60s for first fix ----
        if uart is not None and nmea_lines > 0:
            print()
            print("[11] Waiting up to 60 seconds for first fix...")
            print("     (a fix needs clear sky view; indoors may never work)")
            g = Gps(send_interval_s=30)
            t0 = time.time()
            last_print = 0
            while time.time() - t0 < 60:
                # The Gps class drains UART internally
                fix = g.poll()
                if g.has_fix():
                    print()
                    print("    FIX ACQUIRED!")
                    latest = g.latest()
                    print("    lat = {}".format(latest.get('lat')))
                    print("    lon = {}".format(latest.get('lon')))
                    if 'alt' in latest:
                        print("    alt = {} m".format(latest['alt']))
                    if 'spd' in latest:
                        print("    spd = {:.2f} km/h".format(latest['spd']))
                    break
                # Heartbeat every 10s so the user sees progress
                elapsed = int(time.time() - t0)
                if elapsed >= last_print + 10:
                    last_print = elapsed
                    print("    {}s elapsed, still no valid fix...".format(elapsed))
                time.sleep(0.5)
            else:
                print()
                print("    [!] No fix within 60 seconds.")
                print("        Move the antenna near a window or outside.")
                print("        Cold start can take 2-15 minutes.")
        elif uart is not None and nmea_lines == 0:
            print("\n[11] skipped (no NMEA data)")

    else:
        print()
        print("Hardware diagnostics skipped (running on CPython, not Pico).")

    print()
    if failures == 0:
        print("ALL SELF-TESTS PASSED")
    else:
        print("{} FAILURES".format(failures))