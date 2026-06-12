"""
clock_rtc.py - DS1302 read helpers.

Wraps the bit-banged DS1302 functions from hardware_test.py so the bridge
modules don't need to import the whole test file.

Functions:
    now_str()        -- "YYYY-MM-DD HH:MM:SS" or "uptime+Ns" if RTC unset
    now_hm()         -- (hour, minute, second) or fallback from ticks
    today_tuple()    -- (year, month, day) or None if RTC clearly unset
"""

import time

try:
    import hardware_test as ht
    _HAS_HT = True
except Exception:
    _HAS_HT = False


def now_str():
    """Returns timestamp string for log lines."""
    if _HAS_HT:
        try:
            y, mo, d, h, mi, s, _ = ht.rtc_read_all()
            return "{:04d}-{:02d}-{:02d} {:02d}:{:02d}:{:02d}".format(
                y, mo, d, h, mi, s)
        except Exception:
            pass
    return "uptime+{}s".format(time.ticks_ms() // 1000)


def now_hm():
    """Returns (hour, minute, second). Falls back to uptime if RTC missing."""
    if _HAS_HT:
        try:
            _, _, _, h, mi, s, _ = ht.rtc_read_all()
            return (h, mi, s)
        except Exception:
            pass
    secs = time.ticks_ms() // 1000
    return (secs // 3600 % 24, secs // 60 % 60, secs % 60)


def today_tuple():
    """Returns (year, month, day) or None if RTC obviously unset."""
    if _HAS_HT:
        try:
            y, mo, d, _, _, _, _ = ht.rtc_read_all()
            if 2024 <= y <= 2099 and 1 <= mo <= 12 and 1 <= d <= 31:
                return (y, mo, d)
        except Exception:
            pass
    return None


def set_now(year, month, day, hour, minute, second):
    """Write the given local time to the DS1302. Returns True/False."""
    if not _HAS_HT:
        return False
    try:
        ht.rtc_write_all(year, month, day, hour, minute, second)
        return True
    except Exception:
        return False


def parse_iso(s):
    """Parse 'YYYY-MM-DDTHH:MM:SS' -> (y,mo,d,h,mi,s) or None on failure."""
    s = s.strip()
    if len(s) < 19 or s[4] != '-' or s[7] != '-' \
            or s[10] != 'T' or s[13] != ':' or s[16] != ':':
        return None
    try:
        y  = int(s[0:4])
        mo = int(s[5:7])
        d  = int(s[8:10])
        h  = int(s[11:13])
        mi = int(s[14:16])
        sec = int(s[17:19])
    except ValueError:
        return None
    # Range check
    if not (2000 <= y <= 2099 and 1 <= mo <= 12 and 1 <= d <= 31
            and 0 <= h <= 23 and 0 <= mi <= 59 and 0 <= sec <= 59):
        return None
    return (y, mo, d, h, mi, sec)