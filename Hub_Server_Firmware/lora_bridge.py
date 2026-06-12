"""
bridge.py - Pico A's main bridge loop.

Runs forever, doing all of these in parallel each iteration:
    1. Read USB-serial commands from Pi 5 (TX:, PING, RESET)
    2. Poll LoRa for incoming packets, decrypt, forward to Pi 5
    3. Read buttons (left/right scroll history)
    4. Tick the UI state machine (clock / message / alert)
    5. Expire LED blinks
    6. Hourly log prune

Public entry point:
    bridge.run()   -- never returns; sits in the main loop forever

Self-test (when this file is executed directly):
    Verifies the encrypt/decrypt roundtrip and parses a couple of test
    serial commands. Does NOT touch real hardware (would need a Pico).
"""

import time
import machine
import cryptolib

import config
import leds
import display
import messages
import storage
import serial_io
import ui
import clock_rtc
from buttons import Button
from lora import LoRa


# ============================================================
# Encryption (AES-128 ECB) - MUST match key on Pico B's runtime.py
# ============================================================
LORA_KEY = b"LoRaMeshDemoKey1"   # exactly 16 bytes
_AES_ECB = 1
_BLOCK   = 16


def _unpad(data):
    if not data or len(data) % _BLOCK != 0:
        return None
    n = data[-1]
    if n < 1 or n > _BLOCK:
        return None
    for i in range(1, n + 1):
        if data[-i] != n:
            return None
    return data[:-n]


def _pad(data):
    n = _BLOCK - (len(data) % _BLOCK)
    return data + bytes([n]) * n


def encrypt(plaintext):
    """Encrypt bytes/str -> ciphertext bytes."""
    if isinstance(plaintext, str):
        plaintext = plaintext.encode("utf-8")
    cipher = cryptolib.aes(LORA_KEY, _AES_ECB)
    return cipher.encrypt(_pad(plaintext))


def decrypt(blob):
    """Decrypt bytes -> plaintext bytes, or None on failure."""
    if not isinstance(blob, (bytes, bytearray)):
        return None
    if len(blob) < _BLOCK or len(blob) % _BLOCK != 0:
        return None
    try:
        cipher = cryptolib.aes(LORA_KEY, _AES_ECB)
        padded = cipher.decrypt(bytes(blob))
    except Exception:
        return None
    return _unpad(padded)


# ============================================================
# Device identity + liveness roster
# ============================================================
# The bridge represents the hub on the air. It announces DEVICE: every 60s
# like the other nodes so PicoB/ESP32 know the hub is alive, and feeds the
# same announce up to the Pi so HubServer shows in the devices table / chat
# presence dots. HUB_NAME must match Hub_Server/hub.py HUB_NAME.
HUB_NAME = "HubServer"
try:
    HUB_HWID = machine.unique_id().hex()
except Exception:
    HUB_HWID = "0" * 12

ALIVE_INTERVAL_MS = 60_000

# hwid -> [name, last_seen_ticks_ms]. Updated on every DEVICE: announce we
# hear. The bridge already relays those to the Pi; this just lets Pico A
# itself know who's alive.
_alive = {}


def _note_alive(hwid, name):
    if hwid:
        _alive[hwid] = [name, time.ticks_ms()]


def _parse_device_announce(text):
    """DEVICE:{...} -> (hwid, name), or (None, None) on parse failure."""
    if not text or not text.startswith("DEVICE:"):
        return (None, None)
    try:
        import json as _json
        obj = _json.loads(text[7:])
    except Exception:
        return (None, None)
    if not isinstance(obj, dict):
        return (None, None)
    return (obj.get("id"), obj.get("name"))


# ============================================================
# GPS message handling
# ============================================================
# GPS messages arrive at every node's cadence. We don't want them
# filling the OLED scroll, history buffer, or buzzer alert — just a
# quick blue blink and direct forward to Pi 5. The hub side (hub.py +
# Trip_Hub) owns any dedup/staleness decisions; the bridge stays dumb
# so QPOS replies (which legitimately repeat the last position) are
# never silently dropped.

GPS_BLINK_MS    = 2000   # 2 second blue flash for GPS RX


def _parse_gps_payload(text):
    """Parse 'GPS:{...}' -> (lat, lon) or None on failure.
    Required keys: lat, lon. Other fields ignored here."""
    if not text or not text.startswith("GPS:"):
        return None
    body = text[4:]
    try:
        import json as _json
        obj = _json.loads(body)
    except Exception:
        return None
    if not isinstance(obj, dict):
        return None
    try:
        return (float(obj["lat"]), float(obj["lon"]))
    except (KeyError, TypeError, ValueError):
        return None


def _approx_distance_m(lat1, lon1, lat2, lon2):
    """Approximate distance in meters between two points.
    Equirectangular projection - good enough for short distances (<50km).
    Avoids math.acos, math.cos in an infinite series, etc. - just basic ops."""
    # 1 degree latitude ~= 111320 m, longitude depends on cos(latitude)
    # We use the average latitude so the cos correction is symmetric.
    avg_lat_rad = (lat1 + lat2) * 0.5 * 0.0174532925  # *pi/180
    # Cheap cos approximation: 1 - x^2/2 (good for |x| < ~0.5)
    # For our use (latitude up to ~85 deg = 1.48 rad) we should use real cos:
    try:
        import math
        cos_lat = math.cos(avg_lat_rad)
    except Exception:
        cos_lat = 1.0
    dlat = (lat2 - lat1) * 111320.0
    dlon = (lon2 - lon1) * 111320.0 * cos_lat
    # Simple Euclidean distance from delta meters
    try:
        import math
        return math.sqrt(dlat * dlat + dlon * dlon)
    except Exception:
        # Fallback if math missing for some reason
        return abs(dlat) + abs(dlon)


# ============================================================
# Main loop
# ============================================================
def run():
    """Initialize OLED + LoRa, then loop forever handling everything."""
    # Init OLED + LoRa
    oled = display.open_oled()
    disp = display.Display(oled)

    try:
        lora = LoRa()
    except Exception as e:
        serial_io.emit("ERR:init:{}".format(e))
        return

    lora.on_tx_start = leds.tx_on
    lora.on_tx_end   = leds.tx_off

    # State
    msg_buffer = messages.MessageBuffer()
    state = ui.Ui(disp, msg_buffer)
    state.to_clock()

    # Buttons
    btn_left  = Button(config.BTN_LEFT)
    btn_right = Button(config.BTN_RIGHT)

    # Announce ready
    serial_io.emit("READY")
    storage.append("BRIDGE_START")
    storage.prune()

    # ------- Serial command handler -------
    def handle_serial(cmd, arg):
        if cmd == "TX":
            try:
                ok = lora.send(encrypt(arg))     # <-- encrypt before sending
                serial_io.emit("OK" if ok else "ERR:tx_timeout")
                storage.append("TX " + arg)
            except Exception as e:
                serial_io.emit("ERR:tx:{}".format(e))
        elif cmd == "PING":
            serial_io.emit("PONG")
        elif cmd == "RESET":
            try:
                lora.reset()
                lora.init_radio()
                serial_io.emit("READY")
            except Exception as e:
                serial_io.emit("ERR:reset:{}".format(e))
        elif cmd == "TIME":
            parsed = clock_rtc.parse_iso(arg)
            if parsed is None:
                serial_io.emit("ERR:time:bad_format")
                return
            if clock_rtc.set_now(*parsed):
                serial_io.emit("OK")
                storage.append("TIME_SYNC " + arg)
            else:
                serial_io.emit("ERR:time:write_failed")

    last_prune = time.ticks_ms()
    # Announce on the first loop iteration (don't wait a full interval).
    last_announce = time.ticks_add(time.ticks_ms(), -ALIVE_INTERVAL_MS)

    # GPS icon deadline - while now < icon_until_ms, OLED shows the
    # navigation arrow instead of the clock.  Set on each GPS RX.
    GPS_ICON_MS = 2000
    icon_until_ms = 0

    while True:
        now = time.ticks_ms()

        # 1. USB-serial input (Pi -> Pico)
        serial_io.poll(handle_serial)

        # 2. LoRa receive
        got = lora.poll_rx()
        if got is not None:
            data, rssi, snr = got

            # Try to decrypt. If it fails, fall through with raw data so we
            # at least see something arrived and can debug.
            decrypted = decrypt(data)
            if decrypted is not None:
                data = decrypted
            else:
                serial_io.emit("LOG:decrypt_failed_showing_raw")

            try:
                text = data.decode("utf-8")
            except UnicodeError:
                text = data.hex()

            # Branch: GPS messages get lightweight handling.
            # No OLED scroll, no buzzer, no history. Blue flash and
            # forward to Pi 5 unconditionally — hub.py owns any dedup
            # or staleness logic.
            gps_coords = _parse_gps_payload(text)
            if gps_coords is not None:
                # 2-second blue flash to signal "Pico B is alive"
                leds.rx_blink(duration_ms=GPS_BLINK_MS)
                # Brief navigation-arrow icon on OLED
                disp.show_gps_icon()
                icon_until_ms = time.ticks_add(now, GPS_ICON_MS)

                # NOTE: no msg_buffer.add() and no state.on_rx() for GPS —
                # GPS broadcasts must not enter the chat history buffer or
                # they flood it (one per cadence, every node). Flash log +
                # Pi forward only.
                storage.append("RX " + text, rssi=rssi, snr=snr)
                serial_io.emit("RX:{}|{}|{:.1f}".format(text, rssi, snr))
            elif text.startswith("TRIPSTART:") or text.startswith("TRIPEND:"):
                # Trip lifecycle messages - same lightweight handling as GPS.
                # Forward to Pi 5 (which routes to /api/trip_event or /api/trip)
                # but don't trigger OLED scroll or ALERT mode.
                leds.rx_blink(duration_ms=GPS_BLINK_MS)   # 2s blue flash
                storage.append("RX " + text, rssi=rssi, snr=snr)
                serial_io.emit("RX:{}|{}|{:.1f}".format(text, rssi, snr))
                # NOTE: deliberately no state.on_rx() and no msg_buffer.add
            elif (text.startswith("SYNC:")
                  or text.startswith("RTRIPS:")
                  or text.startswith("RTRIP:")
                  or text.startswith("RPTS:")):
                # Sync protocol messages FROM Pico B -> forward to Pi 5.
                # Transparent relay: no parsing, no OLED, no state change.
                leds.rx_blink(duration_ms=GPS_BLINK_MS)
                storage.append("RX " + text[:40], rssi=rssi, snr=snr)
                serial_io.emit("RX:{}|{}|{:.1f}".format(text, rssi, snr))
            elif text.startswith("DEVICE:"):
                # Single umbrella keyword for device-management.
                # Payload is JSON: {"id":...} on boot announce, or
                # {"id":..., "name":...} for rename TX from Pi.
                # Bridge is transparent — Pi 5 parses the payload.
                leds.rx_blink(duration_ms=GPS_BLINK_MS)
                # DEVICE: doubles as a liveness heartbeat — note the sender.
                _hw, _nm = _parse_device_announce(text)
                _note_alive(_hw, _nm)
                storage.append("RX " + text[:60], rssi=rssi, snr=snr)
                serial_io.emit("RX:{}|{}|{:.1f}".format(text, rssi, snr))
            elif (text.startswith("QTRIPS:")
                  or text.startswith("QTRIP:")
                  or text.startswith("QPTS:")
                  or text.startswith("ACK:")):
                # Sync protocol messages FROM Pi 5 -> forward to Pico B.
                # Transparent relay: encrypt and re-transmit over LoRa.
                leds.rx_blink(duration_ms=GPS_BLINK_MS)
                storage.append("TX " + text[:40])
                try:
                    lora.send(encrypt(text))
                except Exception as e:
                    serial_io.emit("LOG:sync_relay_failed:{}".format(e))
            elif text.startswith("CHAT:"):
                # Tagged chat message. New wire format includes sender:
                #   CHAT:<sender_name>:<body>
                # We split locally for OLED + storage display, but keep
                # the full prefix on the wire when forwarding to Pi 5
                # so the host can attribute messages to the correct
                # device.
                rest = text[5:]
                colon = rest.find(":")
                if colon > 0:
                    sender    = rest[:colon]
                    chat_body = rest[colon+1:]
                else:
                    sender    = "?"
                    chat_body = rest
                shown = "{}: {}".format(sender, chat_body)

                leds.chat_rx_blink()
                msg_buffer.add(shown, rssi, snr)
                storage.append("RX " + shown, rssi=rssi, snr=snr)
                # Forward the WHOLE original payload (CHAT:sender:body) so
                # Hub_Server can parse the sender and tag DB rows.
                serial_io.emit("RX:{}|{}|{:.1f}".format(text, rssi, snr))

                # Auto-reply PONG with the hub's name as sender. Must
                # match HUB_NAME in Hub_Server/hub.py — every
                # CHAT line emitted on behalf of the Pi 5 hub uses the
                # same identity regardless of who sent it (auto-PONG
                # vs user TX from web UI).
                if chat_body.strip().upper().startswith("PING"):
                    reply_body = "PONG " + clock_rtc.now_str()
                    wire = "CHAT:HubServer:" + reply_body
                    try:
                        lora.send(encrypt(wire))
                        storage.append("TX HubServer:" + reply_body)
                        serial_io.emit("LOG:auto_pong_sent")
                    except Exception as e:
                        serial_io.emit("LOG:auto_pong_failed:{}".format(e))

                state.on_rx(shown)
            else:
                # Unknown / untagged payload. Log + forward to Pi for
                # visibility, but don't poke the OLED, chat history, or
                # buzzer — those are reserved for explicit CHAT: traffic.
                storage.append("RX_UNK " + text[:60], rssi=rssi, snr=snr)
                serial_io.emit("LOG:rx_untagged:{}".format(text[:60]))

        # 3. Buttons
        if btn_left.pressed():
            state.on_button_left()
            serial_io.emit("LOG:btn_left")
        if btn_right.pressed():
            state.on_button_right()
            serial_io.emit("LOG:btn_right")

        # 4. State machine tick (renders OLED, handles auto-transitions).
        # Skip while the GPS icon is showing so it doesn't get overwritten.
        if icon_until_ms and time.ticks_diff(now, icon_until_ms) >= 0:
            # Icon just expired - clear deadline; the next tick will redraw
            # the clock (display._last_minute was invalidated earlier).
            icon_until_ms = 0
        if not icon_until_ms:
            state.tick()

        # 5. LED blink expiry
        leds.update(now)

        # 5b. Liveness heartbeat: announce HubServer every 60s. TX over LoRa
        # so PicoB/ESP32 hear the hub, and feed the same announce up to the
        # Pi (we can't hear our own TX) so HubServer lands in the devices
        # table that drives the chat presence dots.
        if time.ticks_diff(now, last_announce) >= ALIVE_INTERVAL_MS:
            last_announce = now
            dev = 'DEVICE:{"id":"' + HUB_HWID + '","name":"' + HUB_NAME + '"}'
            try:
                lora.send(encrypt(dev))
            except Exception as e:
                serial_io.emit("LOG:alive_tx_failed:{}".format(e))
            serial_io.emit("RX:{}|0|0".format(dev))
            _note_alive(HUB_HWID, HUB_NAME)

        # 6. Hourly log prune
        if time.ticks_diff(now, last_prune) > 3_600_000:
            last_prune = now
            storage.prune()

        # Tiny sleep so the loop doesn't peg CPU
        time.sleep_ms(5)


# ============================================================
# Self-test - runs when this module is executed directly
# Tests only the things that don't need actual hardware
# ============================================================
if __name__ == "__main__":
    print("bridge.py self-test")
    print("-" * 40)

    # 1. Encrypt/decrypt roundtrip
    print("\n[1] Encrypt/decrypt roundtrip:")
    failures = 0
    for plain in [b"bip", b"hello world", b"", b"x" * 100, "unicode test"]:
        try:
            blob = encrypt(plain)
            back = decrypt(blob)
            expected = plain.encode("utf-8") if isinstance(plain, str) else plain
            ok = (back == expected)
            print("    {!r:30s} -> {} bytes -> {!r}  {}".format(
                plain, len(blob), back, "OK" if ok else "FAIL"))
            if not ok:
                failures += 1
        except Exception as e:
            print("    {!r}: exception {}".format(plain, e))
            failures += 1

    # 2. Decrypt rejects bad input
    print("\n[2] Decrypt rejects bad input:")
    for bad in [b"", b"too short", b"X" * 15, b"X" * 16, "not bytes"]:
        result = decrypt(bad)
        ok = (result is None)
        print("    {!r:30s} -> {!r}  {}".format(
            bad, result, "OK" if ok else "FAIL"))
        if not ok:
            failures += 1

    # 3. _parse_gps_payload
    print("\n[3] _parse_gps_payload:")
    cases = [
        ('GPS:{"lat":50.07,"lon":14.43}',                (50.07, 14.43)),
        ('GPS:{"lat":50,"lon":14,"alt":200,"spd":1}',    (50.0, 14.0)),
        ('GPS:{"lat":"abc","lon":14}',                   None),  # non-numeric
        ('GPS:{"lat":50}',                                None),  # missing lon
        ('hello',                                         None),  # not GPS
        ('',                                              None),
        ('GPS:not json',                                  None),
        ('GPS:[]',                                        None),  # not dict
    ]
    for inp, expected in cases:
        got = _parse_gps_payload(inp)
        if expected is None:
            ok = (got is None)
        else:
            ok = (got is not None
                  and abs(got[0] - expected[0]) < 1e-6
                  and abs(got[1] - expected[1]) < 1e-6)
        print("    {!r:55s} -> {}".format(inp, "OK" if ok else "FAIL got=" + str(got)))
        if not ok:
            failures += 1

    # 4. _approx_distance_m
    print("\n[4] _approx_distance_m:")
    # Same point -> 0
    d = _approx_distance_m(50.0755, 14.4378, 50.0755, 14.4378)
    ok = (d < 0.01)
    print("    same point -> {:.3f} m  {}".format(d, "OK" if ok else "FAIL"))
    if not ok: failures += 1

    # 1 degree latitude apart -> ~111 km
    d = _approx_distance_m(50.0, 14.0, 51.0, 14.0)
    ok = (110000 < d < 112000)
    print("    1 deg lat   -> {:.0f} m  {}".format(d, "OK" if ok else "FAIL"))
    if not ok: failures += 1

    # ~5 m apart at Prague latitude (lat diff ~0.000045)
    d = _approx_distance_m(50.0755, 14.4378, 50.07555, 14.4378)
    ok = (4 < d < 6)
    print("    ~5 m apart  -> {:.2f} m  {}".format(d, "OK" if ok else "FAIL"))
    if not ok: failures += 1

    # ~10 m apart in longitude
    d = _approx_distance_m(50.0755, 14.4378, 50.0755, 14.43794)
    ok = (8 < d < 12)
    print("    ~10 m lon   -> {:.2f} m  {}".format(d, "OK" if ok else "FAIL"))
    if not ok: failures += 1

    print()
    if failures == 0:
        print("ALL SELF-TESTS PASSED")
    else:
        print("{} FAILURES".format(failures))