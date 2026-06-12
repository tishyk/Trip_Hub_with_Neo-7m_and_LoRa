"""
serial_io.py - USB-serial protocol bridge.

Protocol:
    Pi -> Pico:  TX:<payload>, PING, RESET, TIME:<YYYY-MM-DDTHH:MM:SS>
    Pico -> Pi:  READY, OK, ERR:<reason>, RX:<text>|<rssi>|<snr>, LOG:<text>

Usage in main loop:
    serial_io.poll(handler)   -- reads incoming, calls handler(cmd, arg)
                                 for each complete line received
    serial_io.emit(line)      -- send a line to the Pi

Non-blocking design: we read one character at a time and accumulate into a
buffer. Only when we see '\n' do we dispatch the line to the handler. This
avoids sys.stdin.readline()'s blocking behavior on partial lines.
"""

import sys
import select


_poller = None
_buf = ""

def _ensure_poller():
    global _poller
    if _poller is None:
        _poller = select.poll()
        _poller.register(sys.stdin, select.POLLIN)
    return _poller


def emit(line):
    """Send a line to the Pi over USB-serial."""
    sys.stdout.write(line + "\n")


def _dispatch(line, handler):
    line = line.strip()
    if not line:
        return
    if line.startswith("TX:"):
        handler("TX", line[3:])
    elif line.startswith("TIME:"):
        handler("TIME", line[5:])
    elif line == "PING":
        handler("PING", "")
    elif line == "RESET":
        handler("RESET", "")
    else:
        handler("UNKNOWN", line)


def poll(handler):
    """
    Drain any available characters from stdin without blocking.
    Buffer them; whenever we see '\n', dispatch the completed line.
    Multiple lines arriving in one poll all get dispatched.
    """
    global _buf
    p = _ensure_poller()
    # Loop so we drain everything available right now
    for _ in range(200):  # safety cap so we can't starve the rest of the loop
        if not p.poll(0):
            return
        try:
            ch = sys.stdin.read(1)
        except Exception:
            return
        if not ch:
            return
        if ch == "\n" or ch == "\r":
            if _buf:
                _dispatch(_buf, handler)
                _buf = ""
        else:
            _buf += ch
            # Cap buffer length to avoid runaway if no newline ever comes
            if len(_buf) > 512:
                _buf = ""


# ============================================================
# Self-test - dispatches some sample lines and prints what handler sees
# ============================================================
if __name__ == "__main__":
    print("serial_io.py self-test (dispatch only, no actual stdin)")
    print("-" * 40)
    seen = []
    def h(cmd, arg):
        seen.append((cmd, arg))
        print("    {} -> ({!r}, {!r})".format(cmd, cmd, arg))

    cases = [
        "TX:hello world",
        "PING",
        "RESET",
        "TIME:2026-05-01T14:30:45",
        "GARBAGE",
        "",
        "TX:",
    ]
    for c in cases:
        print("  input: {!r}".format(c))
        _dispatch(c, h)

    expected = [
        ("TX", "hello world"),
        ("PING", ""),
        ("RESET", ""),
        ("TIME", "2026-05-01T14:30:45"),
        ("UNKNOWN", "GARBAGE"),
        # empty input "" doesn't dispatch (filtered before handler)
        ("TX", ""),  # "TX:" -> empty arg
    ]
    if seen == expected:
        print("\nALL OK")
    else:
        print("\nFAIL")
        print("  expected:", expected)
        print("  got     :", seen)