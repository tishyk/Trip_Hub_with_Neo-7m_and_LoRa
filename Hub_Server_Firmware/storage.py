"""
storage.py - persistent log file on Pico flash.

Functions:
    append(text, rssi=None, snr=None)  -- append a timestamped line
    prune()                             -- drop entries older than 3 days
    read_recent(max_lines=50)           -- return last N lines for inspection

Lines look like:
    2026-04-30 14:30:45 RX hello world [RSSI=-87 SNR=9.5]
"""

import config
import clock_rtc   # provides now_str() and today_tuple()


def _format(text, rssi, snr):
    ts = clock_rtc.now_str()
    meta = ""
    if rssi is not None and snr is not None:
        meta = " [RSSI={} SNR={:.1f}]".format(rssi, snr)
    return "{} {}{}\n".format(ts, text, meta)


def append(text, rssi=None, snr=None):
    """Append a single line to the log file."""
    try:
        with open(config.LOG_FILE, "a") as f:
            f.write(_format(text, rssi, snr))
    except Exception as e:
        print("LOG:log_write_failed:{}".format(e))


def _age_days(date_str, today):
    """Approximate age in days for 'YYYY-MM-DD' vs (y,m,d). -1 = unparseable."""
    try:
        ly, lm, ld = int(date_str[:4]), int(date_str[5:7]), int(date_str[8:10])
    except Exception:
        return -1
    return (today[0] - ly) * 365 + (today[1] - lm) * 30 + (today[2] - ld)


def prune():
    """Remove log entries older than LOG_RETENTION_DAYS.
    Uses a temp file to avoid loading the entire log into RAM at once.
    """
    today = clock_rtc.today_tuple()
    if today is None:
        return
    tmp = config.LOG_FILE + ".tmp"
    changed = False
    try:
        with open(config.LOG_FILE, "r") as src, open(tmp, "w") as dst:
            for line in src:
                age = _age_days(line[:10], today)
                if age < 0 or age <= config.LOG_RETENTION_DAYS:
                    dst.write(line)
                else:
                    changed = True
    except OSError:
        # LOG_FILE doesn't exist yet, remove tmp if created
        try:
            import os as _os
            _os.remove(tmp)
        except OSError:
            pass
        return
    if changed:
        try:
            import os as _os
            _os.remove(config.LOG_FILE)
            _os.rename(tmp, config.LOG_FILE)
        except Exception as e:
            print("LOG:log_prune_failed:{}".format(e))
    else:
        # Nothing pruned — remove temp file
        try:
            import os as _os
            _os.remove(tmp)
        except OSError:
            pass


def read_recent(max_lines=50):
    """Return the last N lines from the log file."""
    try:
        with open(config.LOG_FILE, "r") as f:
            return f.readlines()[-max_lines:]
    except OSError:
        return []