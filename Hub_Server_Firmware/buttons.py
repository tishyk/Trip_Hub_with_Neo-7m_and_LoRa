"""
buttons.py - debounced button reader.

Usage:
    btn = Button(gpio=12)
    if btn.pressed():    # True once per press
        ...
"""

import time
from machine import Pin


class Button:
    DEBOUNCE_MS = 30

    def __init__(self, gpio):
        self.pin = Pin(gpio, Pin.IN, Pin.PULL_UP)
        self._last = 1
        self._last_change = 0

    def pressed(self):
        """Return True exactly once per press (active-low, debounced)."""
        now = time.ticks_ms()
        v = self.pin.value()
        if v != self._last and \
                time.ticks_diff(now, self._last_change) > self.DEBOUNCE_MS:
            self._last = v
            self._last_change = now
            if v == 0:
                return True
        return False
