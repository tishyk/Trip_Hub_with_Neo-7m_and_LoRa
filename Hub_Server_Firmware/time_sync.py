"""
time_sync.py - Sync DS1302 time from PC over USB serial.

Two ways to use:

1. AUTO at boot:
   In your main.py / hardware_test.py, call:
       import time_sync
       time_sync.auto_sync_at_boot(timeout_s=2)
   Then run sync_pc.py on the PC within 2 seconds of plugging in / booting.

2. MANUAL from REPL:
   In Thonny REPL after Pico is running:
       import time_sync
       time_sync.sync_now()        # uses Thonny PC time via input()
   OR send a time string from PC manually:
       T:2026-04-30T15:42:00

Time format on the wire:  T:YYYY-MM-DDTHH:MM:SS\n
Reply from Pico on success: OK:YYYY-MM-DDTHH:MM:SS\n
Reply from Pico on parse fail: ERR:reason\n
"""

import sys
import time
import select
from machine import Pin

# DS1302 pins - must match your wiring table
RTC_CLK = 2
RTC_DAT = 3
RTC_RST = 4

# ---- DS1302 low-level (duplicated here so this module is standalone) ----
_clk = None
_dat = None
_rst = None

def _init():
    global _clk, _dat, _rst
    if _clk is None:
        _clk = Pin(RTC_CLK, Pin.OUT, value=0)
        _dat = Pin(RTC_DAT, Pin.OUT, value=0)
        _rst = Pin(RTC_RST, Pin.OUT, value=0)

def _wb(b):
    _dat.init(Pin.OUT)
    for i in range(8):
        _dat.value((b >> i) & 1)
        _clk.value(1); time.sleep_us(2)
        _clk.value(0); time.sleep_us(2)

def _i2bcd(n):
    return ((n // 10) << 4) | (n % 10)

def _write_rtc(year, month, day, hour, minute, second, weekday=1):
    _init()
    # Disable write-protect
    _rst.value(1); time.sleep_us(5)
    _wb(0x8E); _wb(0x00)
    _rst.value(0); time.sleep_us(5)
    # Burst write all 8 registers
    _rst.value(1); time.sleep_us(5)
    _wb(0xBE)
    _wb(_i2bcd(second) & 0x7F)   # CH=0 starts the clock
    _wb(_i2bcd(minute))
    _wb(_i2bcd(hour) & 0x3F)     # 24h mode
    _wb(_i2bcd(day))
    _wb(_i2bcd(month))
    _wb(_i2bcd(weekday) & 0x07)
    _wb(_i2bcd(year - 2000))
    _wb(0x00)
    _rst.value(0)


def _parse_time_str(s):
    """Parse 'T:YYYY-MM-DDTHH:MM:SS' -> (Y,M,D,h,m,s) or raise."""
    s = s.strip()
    if not s.startswith("T:"):
        raise ValueError("missing T: prefix")
    body = s[2:]
    # Expect: YYYY-MM-DDTHH:MM:SS
    if len(body) != 19 or body[4] != '-' or body[7] != '-' \
            or body[10] != 'T' or body[13] != ':' or body[16] != ':':
        raise ValueError("bad format, expected YYYY-MM-DDTHH:MM:SS")
    y  = int(body[0:4])
    mo = int(body[5:7])
    d  = int(body[8:10])
    h  = int(body[11:13])
    mi = int(body[14:16])
    s_ = int(body[17:19])
    if not (2000 <= y <= 2099 and 1 <= mo <= 12 and 1 <= d <= 31
            and 0 <= h <= 23 and 0 <= mi <= 59 and 0 <= s_ <= 59):
        raise ValueError("value out of range")
    return (y, mo, d, h, mi, s_)


def _apply_and_reply(line):
    """Parse line, write RTC, send reply over stdout."""
    try:
        y, mo, d, h, mi, s = _parse_time_str(line)
        _write_rtc(y, mo, d, h, mi, s)
        print("OK:{:04d}-{:02d}-{:02d}T{:02d}:{:02d}:{:02d}".format(
            y, mo, d, h, mi, s))
        return True
    except Exception as e:
        print("ERR:{}".format(e))
        return False


def auto_sync_at_boot(timeout_s=2):
    """
    Listen on USB-serial (stdin) for up to `timeout_s` seconds.
    If a line starting with 'T:' arrives, parse and apply it.
    Returns True if sync happened, False on timeout.
    Non-blocking-ish: uses select.poll on sys.stdin.
    """
    poller = select.poll()
    poller.register(sys.stdin, select.POLLIN)
    deadline = time.ticks_add(time.ticks_ms(), int(timeout_s * 1000))
    buf = ""
    print("SYNC_READY")  # signal to PC that we're listening
    while time.ticks_diff(deadline, time.ticks_ms()) > 0:
        events = poller.poll(50)  # 50ms
        if events:
            ch = sys.stdin.read(1)
            if ch is None:
                continue
            if ch == '\n' or ch == '\r':
                if buf.startswith("T:"):
                    return _apply_and_reply(buf)
                buf = ""
            else:
                buf += ch
                if len(buf) > 64:  # runaway
                    buf = ""
    print("SYNC_TIMEOUT")
    return False


def sync_now():
    """
    Manual sync from REPL. Prompts you to type the time string,
    or paste one from your clipboard.
    """
    print("Paste time string (T:YYYY-MM-DDTHH:MM:SS) and press Enter:")
    line = input().strip()
    return _apply_and_reply(line)


def set_manual(year, month, day, hour, minute, second):
    """Direct setter for use in code, e.g. set_manual(2026, 4, 30, 15, 42, 0)"""
    _write_rtc(year, month, day, hour, minute, second)
    print("OK:{:04d}-{:02d}-{:02d}T{:02d}:{:02d}:{:02d}".format(
        year, month, day, hour, minute, second))