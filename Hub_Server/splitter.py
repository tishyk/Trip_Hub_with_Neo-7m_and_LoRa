"""
splitter.py - Routes incoming LoRa payloads to the right destination.

Right now this only handles GPS messages: lines beginning with "GPS:" are
parsed as JSON and appended to gps.log. In a later step the same code path
will also POST to a web server.

Non-GPS lines are returned unchanged so the caller can log them normally.

Public API:
    is_gps(payload_str) -> bool
    handle_gps(payload_str, gps_log_path, on_post=None)
        Parses payload, appends to gps.log. If on_post is provided, calls
        on_post(parsed_dict) so a future caller (server uploader) can
        forward the point.
        Returns the parsed dict on success, or None on parse failure.
    make_server_poster(url) -> callable
        Returns a function suitable for use as on_post. It POSTs each
        record to <url>/api/live_point in a background thread (non-blocking,
        no exceptions propagate, server downtime won't break the splitter).

Self-test:
    Verifies prefix detection and JSON parsing.
"""

import datetime
import json
import os
import queue
import threading
import urllib.error
import urllib.request


GPS_PREFIX = "GPS:"

# All known on-wire protocol prefixes. Used by hub.py to decide
# whether to leave a console-typed line verbatim (so manual sync tests
# like 'QTRIPS:SergiiT' work) or to CHAT-tag free text.
PROTOCOL_PREFIXES = (
    "GPS:", "TRIPSTART:", "TRIPEND:",
    "SYNC:", "RTRIPS:", "RTRIP:", "RPTS:",
    "QTRIPS:", "QTRIP:", "QPTS:", "ACK:",
    "QPOS:", "WHO?",
    "CHAT:",
)


def is_gps(payload):
    """Returns True if the payload looks like a GPS message."""
    return isinstance(payload, str) and payload.startswith(GPS_PREFIX)


def has_protocol_prefix(payload):
    """True if the payload is already tagged with one of the known on-wire
    protocol prefixes. Anything else is free text the console wraps as CHAT:.
    """
    return isinstance(payload, str) and payload.startswith(PROTOCOL_PREFIXES)


def parse_gps(payload):
    """Parse a GPS payload string -> dict, or None on failure.
    Required keys: lat, lon. Optional: ts, alt, spd."""
    if not is_gps(payload):
        return None
    body = payload[len(GPS_PREFIX):]
    try:
        obj = json.loads(body)
    except Exception:
        return None
    if not isinstance(obj, dict):
        return None
    if "lat" not in obj or "lon" not in obj:
        return None
    # Coerce required fields to float; reject if not numeric
    try:
        obj["lat"] = float(obj["lat"])
        obj["lon"] = float(obj["lon"])
    except (TypeError, ValueError):
        return None
    return obj


def _now_iso():
    return datetime.datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


def handle_gps(payload, gps_log_path, rssi=None, snr=None, on_post=None):
    """Parse, append to gps.log, optionally call on_post(parsed_dict).

    Log line format (one JSON object per line, easy to grep/parse):
        {"recv_at":"2026-05-01T14:30:45","lat":50.07,"lon":14.43,
         "ts":1730290015,"alt":205.3,"spd":4.2,"rssi":-60,"snr":9.5}

    Returns the parsed dict, or None if parsing failed.
    """
    obj = parse_gps(payload)
    if obj is None:
        return None

    record = {"recv_at": _now_iso()}
    record.update(obj)
    if rssi is not None: record["rssi"] = rssi
    if snr  is not None: record["snr"] = snr
    # Attribute the live point to a device. From phase 2b+ firmware
    # carries both 'd' (renameable label) and 'hwid' (permanent id);
    # /api/live_point uses whichever is present, resolving the other
    # via the devices table.
    record["source"] = record.get("d") or record.get("device_id")
    if "hwid" in record:
        record["device_hwid"] = record["hwid"]

    if gps_log_path:
        try:
            with open(gps_log_path, "a") as f:
                f.write(json.dumps(record) + "\n")
        except Exception:
            pass

    if on_post is not None:
        try:
            on_post(record)
        except Exception:
            pass

    return obj


# ============================================================
# Server poster - non-blocking HTTP POST to /api/live_point
# ============================================================
def make_server_poster(url, log_callback=None):
    """Create an on_post callable that ships points to a web server.

    url: base URL of the server, e.g. "http://localhost:5000"
    log_callback: optional fn(level, text) for logging post outcomes
                  (level is "GPS" on success, "ERR" on failure)

    POSTs run in a single background worker thread with a small queue.
    If the server is down, the queue caps at 50 items - oldest dropped.
    """
    q = queue.Queue(maxsize=50)
    endpoint = url.rstrip("/") + "/api/live_point"

    def _log(level, text):
        if log_callback:
            try:
                log_callback(level, text)
            except Exception:
                pass

    def _worker():
        while True:
            record = q.get()
            try:
                body = json.dumps(record).encode("utf-8")
                req = urllib.request.Request(
                    endpoint, data=body, method="POST",
                    headers={"Content-Type": "application/json"})
                with urllib.request.urlopen(req, timeout=3.0) as resp:
                    if 200 <= resp.status < 300:
                        _log("GPS", "posted_to_server")
                    else:
                        _log("ERR", "post_status:{}".format(resp.status))
            except urllib.error.URLError as e:
                _log("ERR", "post_failed:{}".format(e.reason))
            except Exception as e:
                _log("ERR", "post_exception:{}".format(e))

    t = threading.Thread(target=_worker, daemon=True)
    t.start()

    def post(record):
        try:
            q.put_nowait(record)
        except queue.Full:
            # Drop oldest, push newest. Best-effort, no error.
            try:
                q.get_nowait()
                q.put_nowait(record)
            except Exception:
                pass

    return post


# ============================================================
# Trip messages: TRIPSTART / TRIPEND
# ============================================================
TRIPSTART_PREFIX = "TRIPSTART:"
TRIPEND_PREFIX   = "TRIPEND:"


def is_trip(payload):
    """Returns True if payload is a TRIP-related message."""
    return isinstance(payload, str) and (
        payload.startswith(TRIPSTART_PREFIX) or
        payload.startswith(TRIPEND_PREFIX)
    )


def parse_trip(payload):
    """Parse a 'TRIPSTART:{...}' or 'TRIPEND:{...}' payload.

    Returns (kind, dict) where kind is 'TRIPSTART' or 'TRIPEND', or
    (None, None) on parse failure.
    """
    if not isinstance(payload, str):
        return (None, None)
    if payload.startswith(TRIPSTART_PREFIX):
        body = payload[len(TRIPSTART_PREFIX):]
        kind = "TRIPSTART"
    elif payload.startswith(TRIPEND_PREFIX):
        body = payload[len(TRIPEND_PREFIX):]
        kind = "TRIPEND"
    else:
        return (None, None)
    try:
        obj = json.loads(body)
    except Exception:
        return (None, None)
    if not isinstance(obj, dict):
        return (None, None)
    return (kind, obj)


def make_trip_poster(url, log_callback=None):
    """Create a callable that ships TRIPSTART/TRIPEND objects to the server.

    url: base URL of the server.
    Returns post(kind, obj) where kind is 'TRIPSTART' or 'TRIPEND'.
    On 'TRIPSTART' it POSTs to /api/trip_event with type='TRIPSTART'.
    On 'TRIPEND'   it POSTs to /api/trip.
    Background-thread, queued, non-blocking, mirroring make_server_poster.
    """
    q = queue.Queue(maxsize=50)
    base = url.rstrip("/")

    def _log(level, text):
        if log_callback:
            try:
                log_callback(level, text)
            except Exception:
                pass

    def _post(endpoint, body):
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(
            endpoint, data=data, method="POST",
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=3.0) as resp:
            return resp.status

    def _worker():
        while True:
            kind, obj = q.get()
            try:
                if kind == "TRIPSTART":
                    body = dict(obj)
                    body.setdefault("type", "TRIPSTART")
                    status = _post(base + "/api/trip_event", body)
                    if 200 <= status < 300:
                        _log("TRIP", "tripstart_posted")
                    else:
                        _log("ERR", "tripstart_status:{}".format(status))
                elif kind == "TRIPEND":
                    status = _post(base + "/api/trip", obj)
                    if 200 <= status < 300:
                        _log("TRIP", "tripend_posted")
                    else:
                        _log("ERR", "tripend_status:{}".format(status))
                else:
                    _log("ERR", "trip_unknown_kind:{}".format(kind))
            except urllib.error.URLError as e:
                _log("ERR", "trip_post_failed:{}".format(e.reason))
            except Exception as e:
                _log("ERR", "trip_post_exception:{}".format(e))

    t = threading.Thread(target=_worker, daemon=True)
    t.start()

    def post(kind, obj):
        try:
            q.put_nowait((kind, obj))
        except queue.Full:
            try:
                q.get_nowait()
                q.put_nowait((kind, obj))
            except Exception:
                pass

    return post


def handle_trip(payload, on_post=None, log_callback=None):
    """Parse a TRIPSTART/TRIPEND payload and forward via on_post(kind, obj).

    Returns (kind, dict) on success or (None, None) on failure.
    """
    kind, obj = parse_trip(payload)
    if kind is None:
        return (None, None)
    if on_post is not None:
        try:
            on_post(kind, obj)
        except Exception as e:
            if log_callback:
                log_callback("ERR", "trip_on_post:{}".format(e))
    return (kind, obj)


# ============================================================
# Self-test
# ============================================================
if __name__ == "__main__":
    import tempfile

    print("splitter.py self-test")
    print("-" * 40)

    failures = 0

    # is_gps
    print("\n[1] is_gps detection:")
    cases = [
        ('GPS:{"lat":50,"lon":14}', True),
        ('hello', False),
        ('', False),
        ('GPS', False),  # no colon
        ('gps:{}', False),  # case sensitive
    ]
    for inp, expected in cases:
        got = is_gps(inp)
        ok = (got == expected)
        print("    {!r:40s} -> {}  {}".format(inp, got, "OK" if ok else "FAIL"))
        if not ok: failures += 1

    # parse_gps
    print("\n[2] parse_gps:")
    cases2 = [
        ('GPS:{"lat":50.07,"lon":14.43}',                      {"lat":50.07, "lon":14.43}),
        ('GPS:{"lat":50,"lon":14,"alt":200,"spd":4,"ts":1}',  {"lat":50.0, "lon":14.0, "alt":200, "spd":4, "ts":1}),
        ('GPS:{"lat":50}',                                     None),  # missing lon
        ('GPS:{"lon":14}',                                     None),  # missing lat
        ('GPS:not json',                                       None),
        ('GPS:[]',                                             None),  # not dict
        ('GPS:{"lat":"abc","lon":14}',                         None),  # non-numeric lat
        ('hello',                                              None),  # not GPS
    ]
    for inp, expected in cases2:
        got = parse_gps(inp)
        if expected is None:
            ok = (got is None)
        else:
            ok = (got is not None and
                  abs(got["lat"] - expected["lat"]) < 1e-6 and
                  abs(got["lon"] - expected["lon"]) < 1e-6)
        print("    {!r:55s} -> ok={}".format(inp, "YES" if ok else "NO"))
        if not ok: failures += 1

    # handle_gps writes to file
    print("\n[3] handle_gps writes log:")
    with tempfile.NamedTemporaryFile("w", suffix=".log", delete=False) as f:
        log_path = f.name
    try:
        captured = []
        def on_post(rec): captured.append(rec)

        result = handle_gps('GPS:{"lat":50.07,"lon":14.43,"alt":200}',
                            log_path, rssi=-60, snr=9.5, on_post=on_post)
        ok = (result is not None and result["lat"] == 50.07
              and len(captured) == 1
              and captured[0]["rssi"] == -60)
        print("    handle_gps with valid GPS  -> ok={}".format("YES" if ok else "NO"))
        if not ok: failures += 1

        # Check file content
        with open(log_path) as fh:
            lines = fh.read().strip().split("\n")
        ok = (len(lines) == 1)
        print("    gps.log has 1 line         -> ok={}".format("YES" if ok else "NO"))
        if not ok: failures += 1

        rec = json.loads(lines[0])
        ok = (rec["lat"] == 50.07 and rec["lon"] == 14.43
              and rec["alt"] == 200 and rec["rssi"] == -60
              and "recv_at" in rec)
        print("    log record fields correct  -> ok={}".format("YES" if ok else "NO"))
        if not ok: failures += 1

        # Bad GPS - should not write
        result = handle_gps('GPS:{"lat":"oops"}', log_path, on_post=on_post)
        ok = (result is None and len(captured) == 1)  # captured count unchanged
        print("    handle_gps with invalid    -> ok={}".format("YES" if ok else "NO"))
        if not ok: failures += 1
    finally:
        os.unlink(log_path)

    # make_server_poster - non-blocking even when server is unreachable
    print("\n[4] make_server_poster (no-server, must not crash):")
    import time as _time
    log_seen = []
    def _logcb(level, text): log_seen.append((level, text))
    poster = make_server_poster("http://127.0.0.1:1",  # nothing listening
                                log_callback=_logcb)
    poster({"lat": 50.0, "lon": 14.0})
    poster({"lat": 51.0, "lon": 15.0})
    _time.sleep(0.5)  # let worker thread try (and fail)
    # We don't assert on the error message, just that the call returned
    # synchronously and the worker logged something.
    ok = (len(log_seen) >= 1 and log_seen[0][0] == "ERR")
    print("    poster failures logged     -> ok={}  ({} log events)".format(
        "YES" if ok else "NO", len(log_seen)))
    if not ok: failures += 1

    # is_trip
    print("\n[5] is_trip:")
    cases3 = [
        ('TRIPSTART:{"device":"B1"}', True),
        ('TRIPEND:{"device":"B1"}',   True),
        ('GPS:{"lat":50}',             False),
        ('hello',                      False),
        ('',                           False),
    ]
    for inp, expected in cases3:
        got = is_trip(inp)
        ok = (got == expected)
        print("    {!r:40s} -> {}  {}".format(inp, got, "OK" if ok else "FAIL"))
        if not ok: failures += 1

    # parse_trip
    print("\n[6] parse_trip:")
    cases4 = [
        ('TRIPSTART:{"device":"B1","ts":1730290015,"lat":50.07,"lon":14.43}',
            ("TRIPSTART", {"device":"B1","ts":1730290015,"lat":50.07,"lon":14.43})),
        ('TRIPEND:{"device":"B1","sts":1,"ets":2,"slat":50.0,"slon":14.0,"elat":50.1,"elon":14.1,"km":1.2,"dur":300,"type":"walking","avg":4.1,"max":5.8}',
            ("TRIPEND", None)),  # we'll just check kind
        ('TRIPSTART:not json', (None, None)),
        ('TRIPSTART:[]',       (None, None)),
        ('hello',              (None, None)),
    ]
    for inp, (exp_kind, exp_obj) in cases4:
        got_kind, got_obj = parse_trip(inp)
        ok = (got_kind == exp_kind)
        if exp_obj is not None and got_obj is not None:
            for k, v in exp_obj.items():
                if got_obj.get(k) != v:
                    ok = False
                    break
        elif exp_obj is None and exp_kind is None and got_obj is not None:
            ok = False
        print("    kind={!s:9s} from {!r:60s} {}".format(
            got_kind, inp[:55] + ('...' if len(inp) > 55 else ''),
            "OK" if ok else "FAIL"))
        if not ok: failures += 1

    print()
    if failures == 0:
        print("ALL SELF-TESTS PASSED")
    else:
        print("{} FAILURES".format(failures))