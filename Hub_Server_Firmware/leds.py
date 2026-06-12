"""
leds.py - LED patterns for the bridge UI.

Functions:
    rx_blink()         -- short blue flash on GPS / generic RX activity
    chat_rx_blink()    -- 2-second green flash on chat RX (text from peer)
    tx_on() / tx_off() -- green LEDs steady during TX (~300 ms minimum)
    alert_start()      -- start generic alternating red flash (timer-driven)
    alert_stop()       -- stop red flash
    msg_alert_start()  -- start unread-message alert (alternating green,
                          loop-driven so it coexists with chat_rx_blink)
    msg_alert_stop()   -- stop unread-message alert
    update(now_ms)     -- call every loop tick to expire short blinks
                          and to advance msg_alert toggling
    all_off()          -- turn everything off

Color meaning:
    BLUE   = GPS RX (every 30 s when fix is active)
    GREEN  = chat activity (RX 2 s flash, TX ~300 ms) AND missed-message
             alert (alternating, MSG_ALERT_FLASH_MS period)
    RED    = generic alert primitive (alternating timer-driven) — kept
             available for any caller that wants it; the UI's missed-
             message notification now uses the green msg_alert instead.
"""

import time
from machine import Pin, Timer
import config

# ---- pin cache so we don't re-init Pin objects ----
_pins = {}
def _p(gpio):
    if gpio not in _pins:
        _pins[gpio] = Pin(gpio, Pin.OUT)
    return _pins[gpio]

# Pre-cache pin objects we use in the alert ISR. ISRs in MicroPython
# cannot allocate memory (no dict lookups, no creating new objects),
# so we resolve these once at module load.
_RED_R_PIN = Pin(config.RED_RIGHT, Pin.OUT)
_RED_L_PIN = Pin(config.RED_LEFT,  Pin.OUT)
_pins[config.RED_RIGHT] = _RED_R_PIN
_pins[config.RED_LEFT]  = _RED_L_PIN

# Pre-cache green alert pins too. The msg_alert is loop-driven (not ISR),
# so allocation rules don't strictly apply, but caching keeps update()
# hot-path free of dict lookups.
_GREEN_R_PIN = Pin(config.GREEN_RIGHT, Pin.OUT)
_GREEN_L_PIN = Pin(config.GREEN_LEFT,  Pin.OUT)
_pins[config.GREEN_RIGHT] = _GREEN_R_PIN
_pins[config.GREEN_LEFT]  = _GREEN_L_PIN

# ---- state ----
_rx_blink_until      = 0   # blue (GPS / generic RX)
_tx_blink_until      = 0   # green (TX, short)
_chat_rx_blink_until = 0   # green (chat RX, ~2 s)
_alert_timer = None
_alert_side  = 0   # 0 = right red lit, 1 = left red lit

# msg_alert (missed-message): main-loop-driven so it naturally coexists
# with chat_rx_blink which shares the GREEN_LEDS pins.
_msg_alert_active  = False
_msg_alert_side    = 0   # 0 = right green lit, 1 = left green lit
_msg_alert_next_ms = 0


def all_off():
    for g in config.ALL_LEDS:
        _p(g).value(0)


# ---- BLUE: GPS / generic RX (ethernet-style flash) ----
def rx_blink(duration_ms=None):
    """Short blue flash. duration_ms overrides the default (config.RX_BLINK_MS)."""
    global _rx_blink_until
    dur = duration_ms if duration_ms is not None else config.RX_BLINK_MS
    deadline = time.ticks_add(time.ticks_ms(), dur)
    # Extend, never shorten - if a longer flash is in progress, keep it
    if _rx_blink_until == 0 or time.ticks_diff(deadline, _rx_blink_until) > 0:
        _rx_blink_until = deadline
    for g in config.BLUE_LEDS:
        _p(g).value(1)


# ---- GREEN: chat RX (text from peer) ----
CHAT_RX_BLINK_MS = 2000

def chat_rx_blink(duration_ms=None):
    """Green flash on incoming chat (text) message."""
    global _chat_rx_blink_until
    dur = duration_ms if duration_ms is not None else CHAT_RX_BLINK_MS
    deadline = time.ticks_add(time.ticks_ms(), dur)
    if _chat_rx_blink_until == 0 or \
            time.ticks_diff(deadline, _chat_rx_blink_until) > 0:
        _chat_rx_blink_until = deadline
    for g in config.GREEN_LEDS:
        _p(g).value(1)


# ---- GREEN: TX activity ----
# Guarantees a minimum visible blink even for very short transmissions.
TX_MIN_VISIBLE_MS = 300

def tx_on():
    """Turn green LEDs on at TX start."""
    global _tx_blink_until
    _tx_blink_until = time.ticks_add(time.ticks_ms(), TX_MIN_VISIBLE_MS)
    for g in config.GREEN_LEDS:
        _p(g).value(1)

def tx_off():
    """Called when TX completes. Doesn't turn off immediately - keeps green
    on at least TX_MIN_VISIBLE_MS so the blink is visible to the eye."""
    # Just extend the deadline if needed; update() turns them off.
    pass


# ---- Alert: alternating red LEDs, side to side ----
# IMPORTANT: This runs in interrupt context. Use only pre-cached Pin objects
# and avoid any allocation (no dict lookups, no string formatting, no print).
def _alert_step(_t):
    global _alert_side
    _alert_side ^= 1
    if _alert_side == 0:
        _RED_R_PIN.value(1)
        _RED_L_PIN.value(0)
    else:
        _RED_R_PIN.value(0)
        _RED_L_PIN.value(1)

def alert_start():
    global _alert_timer
    if _alert_timer is not None:
        return
    _alert_timer = Timer()
    _alert_timer.init(period=config.ALERT_FLASH_MS,
                      mode=Timer.PERIODIC,
                      callback=_alert_step)

def alert_stop():
    global _alert_timer
    if _alert_timer is not None:
        try: _alert_timer.deinit()
        except: pass
        _alert_timer = None
    _RED_R_PIN.value(0)
    _RED_L_PIN.value(0)


# ---- Missed-message alert: alternating GREEN, loop-driven ----
# Driven from update() instead of a Timer so it naturally yields the GREEN
# LEDs to chat_rx_blink (which lights both greens for 2 s on every RX).
def msg_alert_start():
    """Begin the missed-message alert. Idempotent.

    Pattern: GREEN_LEFT and GREEN_RIGHT alternate every MSG_ALERT_FLASH_MS.
    While chat_rx_blink is active, the toggle is paused so the 2 s green
    flash isn't cut short — the alert resumes once chat_rx_blink expires.
    """
    global _msg_alert_active, _msg_alert_side, _msg_alert_next_ms
    if _msg_alert_active:
        return
    _msg_alert_active = True
    _msg_alert_side = 0
    _msg_alert_next_ms = time.ticks_ms()  # toggle on next update() tick


def msg_alert_stop():
    """End the missed-message alert. Turns the green alert pins off, but
    only if chat_rx_blink isn't currently lighting them — otherwise we'd
    cut the 2 s chat flash short."""
    global _msg_alert_active
    _msg_alert_active = False
    chat_active = (_chat_rx_blink_until and
                   time.ticks_diff(time.ticks_ms(), _chat_rx_blink_until) < 0)
    if not chat_active:
        _GREEN_R_PIN.value(0)
        _GREEN_L_PIN.value(0)


# ---- Per-tick maintenance ----
def update(now_ms=None):
    """Expire short blinks. Call this every main-loop iteration.

    GREEN is shared by TX and chat RX. Only turn it off when BOTH deadlines
    have expired, otherwise we'd cut a long chat-RX flash short whenever a
    quick TX happens during it (or vice versa).
    """
    global _rx_blink_until, _tx_blink_until, _chat_rx_blink_until
    if now_ms is None:
        now_ms = time.ticks_ms()

    # BLUE: independent
    if _rx_blink_until and time.ticks_diff(now_ms, _rx_blink_until) >= 0:
        for g in config.BLUE_LEDS:
            _p(g).value(0)
        _rx_blink_until = 0

    # GREEN: shared between TX, chat RX, and msg_alert.
    tx_active   = (_tx_blink_until      and time.ticks_diff(now_ms, _tx_blink_until)      < 0)
    chat_active = (_chat_rx_blink_until and time.ticks_diff(now_ms, _chat_rx_blink_until) < 0)
    if not tx_active and not chat_active:
        # Both short-blink deadlines expired (or never set). Safe to clear
        # the green pins UNLESS msg_alert is running — in that case let
        # msg_alert manage them so we don't introduce a flicker.
        if _tx_blink_until or _chat_rx_blink_until:
            if not _msg_alert_active:
                for g in config.GREEN_LEDS:
                    _p(g).value(0)
            _tx_blink_until = 0
            _chat_rx_blink_until = 0

    # msg_alert: alternating green flash, paused while chat_rx_blink is
    # lighting both greens (alert resumes after the 2 s chat flash ends).
    if _msg_alert_active and not chat_active:
        global _msg_alert_side, _msg_alert_next_ms
        if time.ticks_diff(now_ms, _msg_alert_next_ms) >= 0:
            _msg_alert_side ^= 1
            if _msg_alert_side == 0:
                _GREEN_R_PIN.value(1)
                _GREEN_L_PIN.value(0)
            else:
                _GREEN_R_PIN.value(0)
                _GREEN_L_PIN.value(1)
            _msg_alert_next_ms = time.ticks_add(now_ms, config.MSG_ALERT_FLASH_MS)