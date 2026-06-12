"""
distance.py - HC-SR04 ultrasonic distance sensor.

Functions:
    measure_cm()    -- single reading in cm, or None on timeout
    poll_cm(period_ms=200)  -- non-blocking-ish wrapper that only
                               actually triggers a reading every period_ms.
                               Returns last reading or None.
"""

import time
from machine import Pin
import config


_trig = None
_echo = None
_last_value_cm = None
_last_poll_t = 0


def _init():
    global _trig, _echo
    if _trig is None:
        _trig = Pin(config.HCSR04_TRIG, Pin.OUT, value=0)
        _echo = Pin(config.HCSR04_ECHO, Pin.IN)


def measure_cm():
    """One blocking reading. Takes ~30ms worst-case. Returns float cm or None."""
    _init()
    _trig.value(0); time.sleep_us(2)
    _trig.value(1); time.sleep_us(10)
    _trig.value(0)

    # Wait for echo to go high
    t0 = time.ticks_us()
    while _echo.value() == 0:
        if time.ticks_diff(time.ticks_us(), t0) > 30000:
            return None  # no echo
    start = time.ticks_us()
    # Wait for echo to go low
    while _echo.value() == 1:
        if time.ticks_diff(time.ticks_us(), start) > 30000:
            return None  # too far / timeout
    end = time.ticks_us()
    dur_us = time.ticks_diff(end, start)
    return (dur_us * 0.0343) / 2  # speed of sound, cm


def poll_cm(period_ms=200):
    """Returns latest reading; only triggers a new measurement every period_ms.
    Call this freely from the main loop - it'll skip cheap when not yet due."""
    global _last_value_cm, _last_poll_t
    now = time.ticks_ms()
    if time.ticks_diff(now, _last_poll_t) < period_ms:
        return _last_value_cm
    _last_poll_t = now
    _last_value_cm = measure_cm()
    return _last_value_cm