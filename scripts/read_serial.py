#!/usr/bin/env python3
"""Read a device's serial output for a few seconds.

Usage:
    python scripts/read_serial.py COM9 --no-reset --seconds 6
    python scripts/read_serial.py /dev/ttyACM0 --baud 115200

--no-reset opens the port WITHOUT asserting DTR/RTS, so it won't reboot a board
that resets on those lines (notably the ESP32-C3's native USB). Requires pyserial.
"""
import argparse
import sys
import time


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("port", help="serial port, e.g. COM9 or /dev/ttyACM0")
    ap.add_argument("--baud", type=int, default=115200)
    ap.add_argument("--seconds", type=float, default=6.0)
    ap.add_argument("--no-reset", action="store_true",
                    help="don't assert DTR/RTS (avoid rebooting the ESP32-C3)")
    args = ap.parse_args()

    try:
        import serial
    except ImportError:
        sys.exit("pyserial not installed. Run: pip install pyserial")

    ser = serial.Serial()
    ser.port = args.port
    ser.baudrate = args.baud
    ser.timeout = 0.2
    if args.no_reset:
        # Configure the control lines low BEFORE opening so the open doesn't
        # pulse them (which resets boards that reset on DTR/RTS).
        ser.dtr = False
        ser.rts = False
    ser.open()

    end = time.time() + args.seconds
    try:
        while time.time() < end:
            data = ser.read(4096)
            if data:
                sys.stdout.write(data.decode("utf-8", "replace"))
                sys.stdout.flush()
    finally:
        ser.close()


if __name__ == "__main__":
    main()
