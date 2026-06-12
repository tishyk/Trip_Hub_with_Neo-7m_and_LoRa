"""
main.py - autostart for Pico B (no Thonny attached, e.g. battery / power bank).

Boot sequence:
  1. Patch print() and sys.stdout.write() so they swallow exceptions.
     (Without this, MicroPython prints can hang the program when the
     USB serial buffer fills up because no host is reading.)
  2. Two quick onboard-LED blinks so you can see Pico is alive.
  3. Set STANDALONE flag so runtime skips REPL/prompt logic.
  4. Run runtime.chat().  If it raises, append the traceback to
     boot_errors.log on the Pico's flash, wait 5s, soft-reboot.

To go back to interactive Thonny use:
  - Hold the BOOTSEL button + reset, or
  - Stop the running script in Thonny and rename main.py -> main.bak
"""

import sys
import time
import builtins
import machine

# ---------- 1. Safe print --------------------------------------------------
# When no host is reading USB serial, the output buffer fills and the
# next print() blocks forever.  Wrap them in try/except so they no-op
# instead of hanging the whole loop.

_orig_print = builtins.print
def _safe_print(*args, **kwargs):
    try:
        _orig_print(*args, **kwargs)
    except Exception:
        pass
builtins.print = _safe_print

_orig_write = sys.stdout.write
def _safe_write(s):
    try:
        return _orig_write(s)
    except Exception:
        return 0
try:
    sys.stdout.write = _safe_write   # may not be assignable on all builds
except Exception:
    pass

# ---------- 2. LED heartbeat ----------------------------------------------
try:
    led = machine.Pin(25, machine.Pin.OUT)
    for _ in range(2):
        led.value(1); time.sleep(0.1)
        led.value(0); time.sleep(0.1)
except Exception:
    pass

# ---------- 3 + 4. Run runtime in standalone mode -------------------------
def _log_error(text):
    """Append a line to boot_errors.log.  Best-effort."""
    try:
        with open("boot_errors.log", "a") as f:
            f.write(text)
            f.write("\n")
    except Exception:
        pass

try:
    import runtime
    runtime.STANDALONE = True
    runtime.chat()
except Exception as e:
    # Capture the error so it can be inspected later via Thonny.
    try:
        import io
        try:
            buf = io.StringIO()
        except Exception:
            buf = None
        ts = time.localtime()
        when = "{:04d}-{:02d}-{:02d}T{:02d}:{:02d}:{:02d}".format(
            ts[0], ts[1], ts[2], ts[3], ts[4], ts[5])
        _log_error("---- crash @ {} ----".format(when))
        _log_error("error: {}".format(repr(e)))
        if buf is not None:
            try:
                sys.print_exception(e, buf)
                _log_error(buf.getvalue())
            except Exception:
                pass
    finally:
        # Wait then soft-reboot so we don't burn power in a tight crash loop.
        time.sleep(5)
        machine.soft_reset()