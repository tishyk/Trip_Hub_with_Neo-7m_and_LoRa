"""
ui.py - top-level UI state machine.

States:
    CLOCK   -- showing the clock, idle
    MESSAGE -- a message is scrolling on the screen
               Stays on screen at least MSG_LIFETIME_MS, AND while something
               is within PROXIMITY_HOLD_CM. Walks away -> clock.
    ALERT   -- clock is back, but a message went unread:
               red LEDs alternate + buzzer beeps periodically until ack

Transitions:
    on_rx(text)             RX from radio  -> MESSAGE (unacked)
    on_button_left/right    Button press   -> MESSAGE (acked, scroll dir varies)
    tick()                  Time-based     -> auto-transition + render

The state machine doesn't talk to the radio or serial directly. It just
reacts to events the main loop feeds it.
"""

import time
from machine import Pin, PWM
import config
import leds
import distance


STATE_CLOCK   = 0
STATE_MESSAGE = 1
STATE_ALERT   = 2


def _beep_once():
    """Short beep, blocks ~80ms."""
    try:
        buz = PWM(Pin(config.BUZZER))
        buz.freq(2000)
        buz.duty_u16(20000)
        time.sleep_ms(80)
        buz.duty_u16(0)
        buz.deinit()
    except Exception:
        pass


class Ui:
    def __init__(self, display, msg_buffer):
        self.display = display
        self.buffer = msg_buffer

        self.state = STATE_CLOCK
        self.state_since = time.ticks_ms()

        # alert state
        self.acked = True
        self.last_beep_t = 0

        # proximity state
        self.far_hits = 0   # consecutive readings above threshold

        # ticks_ms when the current scroll first completed a full pass, or
        # None while still scrolling. Used to hold MESSAGE state for
        # SCROLL_TAIL_MS after the last char crossed.
        self._scroll_done_at = None

    # ---------- transitions ----------
    def to_clock(self):
        self.state = STATE_CLOCK
        self.state_since = time.ticks_ms()
        leds.msg_alert_stop()
        self.display.show_clock(force=True)

    def to_message(self, text, direction=-1, acked=False):
        self.state = STATE_MESSAGE
        self.state_since = time.ticks_ms()
        self.acked = acked
        self.far_hits = 0
        self._scroll_done_at = None
        self.display.start_scroll(text, direction=direction)
        leds.msg_alert_stop()

    def to_alert(self):
        self.state = STATE_ALERT
        self.state_since = time.ticks_ms()
        self.acked = False
        self.last_beep_t = 0
        leds.msg_alert_start()
        # No beep here - we beep ONCE on arrival in on_rx() instead.
        # ALERT mode is silent (only the green msg_alert flashing).
        self.display.show_clock(force=True)

    # ---------- event handlers ----------
    def on_rx(self, text):
        """Called when a LoRa message arrives."""
        # Single beep at arrival; no further beeps even after entering ALERT.
        _beep_once()
        self.to_message(text, direction=-1, acked=False)

    def on_button_left(self):
        """Previous (older) message in history. Always scrolls leftward."""
        item = self.buffer.older()
        if item is None:
            return
        idx, total = self.buffer.position()
        # Show position prefix so user knows where in history they are.
        # idx 0 = newest, total-1 = oldest.
        if total > 1:
            prefix = "[{}/{}] ".format(idx + 1, total)  # 1-indexed for humans
            text = prefix + item[1]
        else:
            text = item[1]
        self.to_message(text, direction=-1, acked=True)

    def on_button_right(self):
        """Next (newer) message in history. Always scrolls leftward."""
        item = self.buffer.newer()
        if item is None:
            return
        idx, total = self.buffer.position()
        if total > 1:
            prefix = "[{}/{}] ".format(idx + 1, total)
            text = prefix + item[1]
        else:
            text = item[1]
        self.to_message(text, direction=-1, acked=True)

    # ---------- per-tick render + auto transitions ----------
    def tick(self):
        now = time.ticks_ms()
        elapsed = time.ticks_diff(now, self.state_since)

        if self.state == STATE_MESSAGE:
            self._tick_message(now, elapsed)

        elif self.state == STATE_ALERT:
            # Clock visible; red LEDs flash (timer-driven).  No buzzer
            # in ALERT - we only beep once at message arrival in on_rx().
            self.display.show_clock()
            # Proximity wave dismisses the alert and re-shows the message
            d = distance.poll_cm(period_ms=config.PROXIMITY_POLL_MS)
            if d is not None and d <= config.ALERT_DISMISS_CM:
                latest = self.buffer.latest()
                if latest is not None:
                    # Same effect as pressing left button: scroll the latest
                    # message and mark as acked.
                    self.to_message(latest[1], direction=-1, acked=True)

        else:  # CLOCK
            self.display.show_clock()

    def _tick_message(self, now, elapsed):
        """Decide whether to keep showing the message, switch to clock, or
        switch to alert. Renders one frame of the scroll if we're staying.

        Lifetime model: hold MESSAGE state until the text has scrolled fully
        across the OLED at least once AND SCROLL_TAIL_MS has elapsed since
        that point. Proximity then keeps it on screen as long as someone is
        close. This guarantees the user always sees the full message,
        regardless of length.
        """

        # Check proximity (only triggers a reading every PROXIMITY_POLL_MS)
        d = distance.poll_cm(period_ms=config.PROXIMITY_POLL_MS)
        someone_close = (d is not None and d <= config.PROXIMITY_HOLD_CM)

        if someone_close:
            self.far_hits = 0
        else:
            # Either nothing is close, or sensor returned None (out of range).
            # Treat None as "far" but only after several consecutive misses
            # so brief sensor glitches don't kick the user off.
            self.far_hits += 1

        # 1) Always finish at least one full scroll pass.
        if not self.display.scroll_done():
            self.display.scroll_step()
            return

        # 2) Hold for SCROLL_TAIL_MS after the scroll first completes.
        if self._scroll_done_at is None:
            self._scroll_done_at = now
        if time.ticks_diff(now, self._scroll_done_at) < config.SCROLL_TAIL_MS:
            self.display.scroll_step()
            return

        # 3) Scroll + tail done. Keep showing while someone is close.
        if self.far_hits < config.PROXIMITY_FAR_HITS:
            self.display.scroll_step()
            return

        # 4) Far enough for long enough — exit MESSAGE state.
        if self.acked:
            self.to_clock()
        else:
            self.to_alert()