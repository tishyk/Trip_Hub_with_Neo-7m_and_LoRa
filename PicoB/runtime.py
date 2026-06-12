"""
runtime.py - Pico B device runtime.

The full node-side program: LoRa radio + AES, GPS module, trip-tracker
state machine, sync protocol (SYNC/Q*/R*/ACK), chat send/receive,
QPOS/WHO presence handlers, watchdog/retry, cadence-by-class.

main.py imports this and calls chat() on boot (the function name is
historical — chat() now runs the whole node). Pico A pins (old card):
    SCK=GPIO2, MOSI=GPIO3, MISO=GPIO4, NSS=GPIO5, RST=GPIO22, DIO0=GPIO26
Radio settings (must match the bridge): 433 MHz, SF9, BW125, CR4/5,
sync 0x34, CRC on. AES-128-ECB with the network key in config.py.

Hub-side counterpart lives in Hub_Server/hub.py.
"""

import time
import sys
import select
import json
from machine import SPI, Pin
import cryptolib

import gps_module
import trip_tracker
import trip_storage
import sync_codec


# ============================================================
# Device identity (multi-device-friendly)
# ============================================================
# DEVICE_ID is loaded from device_id.txt on flash if present (so the Pi
# can rename the device over LoRa via the DEVICE: protocol). Falls back
# to the compile-time default below for fresh / unrecovered devices.
_DEFAULT_DEVICE_ID = "raspberry-pico"
DEVICE_ID_FILE     = "device_id.txt"

def _load_device_id():
    try:
        with open(DEVICE_ID_FILE, "r") as f:
            new_id = f.read().strip()
        if (new_id and len(new_id) < 16 and
                all((c.isalpha() or c.isdigit() or c in "_-") for c in new_id)):
            return new_id
    except Exception:
        pass
    return _DEFAULT_DEVICE_ID

DEVICE_ID = _load_device_id()
print("device_id={}".format(DEVICE_ID))

# Permanent hardware id, hex-encoded. RP2040 burns 8 bytes of unique
# silicon id at the factory — survives any flash erase and is globally
# unique. The Pi 5 uses this to address rename commands so the
# (renameable) device_id name and the (immutable) device identity stay
# clearly separate on the wire and in the DB.
try:
    from machine import unique_id as _machine_unique_id
    DEVICE_HWID = _machine_unique_id().hex()
except Exception as _e:
    # Should never happen on RP2040, but keep the device functional.
    DEVICE_HWID = "0" * 16
    print("[WARN] unique_id failed:", _e)
print("device_hwid={}".format(DEVICE_HWID))

# Local audit log of every completed trip on this Pico.  Best-effort, no
# rotation - if you walk a lot, prune manually via Thonny.
TRIPS_LOG_FILE = "trips_log.json"

# GPS poll cadence by movement class (seconds).  The save cadence directly
# determines how dense the .gps file will be - faster cadences give a
# smoother polyline on the map but cost a bit more battery and flash.
#
# IDLE is much slower since there's nothing useful to record.
CADENCE_BY_CLASS = {
    "idle":    60,
    "walking": 15,
    "cycling": 10,
    "driving": 10,
}

# Hysteresis bands so a speed bouncing around a boundary doesn't cause us
# to switch cadence on every fix.  Once classified, we only step UP to a
# higher class when speed exceeds upper_band, and step DOWN when speed
# drops below lower_band.
#
# Class boundaries today: walking < 7 < cycling < 25 <= driving.
# We use +/- 1 km/h margins.
WALK_TO_CYCLE_UP      = 8.0
CYCLE_TO_WALK_DOWN    = 6.0
CYCLE_TO_DRIVING_UP   = 27.0
DRIVING_TO_CYCLE_DOWN = 23.0


def pick_class(spd, prev_class):
    """Pick movement class with hysteresis to avoid thrashing.
    spd is in km/h.  prev_class is the previously selected class
    ("walking"/"cycling"/"driving"/"idle"), used to decide the threshold."""
    if prev_class == "walking":
        if spd >= WALK_TO_CYCLE_UP:
            return "cycling" if spd < CYCLE_TO_DRIVING_UP else "driving"
        return "walking"
    if prev_class == "cycling":
        if spd >= CYCLE_TO_DRIVING_UP:
            return "driving"
        if spd < CYCLE_TO_WALK_DOWN:
            return "walking"
        return "cycling"
    if prev_class == "driving":
        if spd < DRIVING_TO_CYCLE_DOWN:
            # Could drop straight to walking if very slow
            if spd < CYCLE_TO_WALK_DOWN:
                return "walking"
            return "cycling"
        return "driving"
    # First call (prev_class None or "idle") - pick by static thresholds
    if spd >= CYCLE_TO_DRIVING_UP:
        return "driving"
    if spd >= WALK_TO_CYCLE_UP:
        return "cycling"
    return "walking"


# Standalone mode: when True, skip stdin/REPL handling and don't draw a
# prompt.  Set this from main.py before calling chat() so the Pico can
# run on battery power with no host attached.
STANDALONE = False


# ============================================================
# Encryption (AES-128 ECB) - MUST match key on Pico A's main.py
# ============================================================
LORA_KEY = b"LoRaMeshDemoKey1"   # exactly 16 bytes - must match Pico A's main.py
_AES_ECB = 1
_BLOCK   = 16

def _pad(data):
    n = _BLOCK - (len(data) % _BLOCK)
    return data + bytes([n]) * n

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

def encrypt(plaintext):
    if isinstance(plaintext, str):
        plaintext = plaintext.encode("utf-8")
    cipher = cryptolib.aes(LORA_KEY, _AES_ECB)
    return cipher.encrypt(_pad(plaintext))

def decrypt(blob):
    """Returns plaintext bytes or None on failure."""
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
# GPS message helper
# ============================================================
# When user types:  gps 50.0755 14.4378
#                   gps 50.0755 14.4378 205.3 4.2
# we wrap as JSON:  GPS:{"lat":50.0755,"lon":14.4378,"alt":205.3,"spd":4.2,"ts":<unix>}
def format_gps_message(parts):
    """parts: list of strings after the 'gps' keyword.
    Returns the formatted message string, or raises ValueError.

    Wire payload is intentionally minimal — the only thing GPS:
    broadcasts feed is the live map dot, which needs identity +
    position + timestamp. Altitude / speed live in the trip log
    (TRIPEND / RPTS fix data), not in periodic broadcasts. Saves
    ~40 B per packet which translates to ~150 ms less airtime at SF9.
    """
    if len(parts) < 2:
        raise ValueError("usage: gps <lat> <lon>")
    obj = {"hwid": DEVICE_HWID,
           "lat":  float(parts[0]),
           "lon":  float(parts[1]),
           "ts":   int(time.time())}
    return "GPS:" + json.dumps(obj, separators=(',', ':'))


def format_gps_from_fix(fix):
    """Same wire format as format_gps_message, but takes a dict from
    gps_module.poll() instead of typed string parts.

    fix: {"lat": ..., "lon": ..., "ts": ..., ...}
         lat/lon required; ts optional (falls back to Pico clock).
    """
    obj = {"hwid": DEVICE_HWID,
           "lat":  round(fix["lat"], 6),
           "lon":  round(fix["lon"], 6),
           "ts":   int(fix.get("ts", time.time()))}
    return "GPS:" + json.dumps(obj, separators=(',', ':'))


# ============================================================
# Trip events: send TRIPSTART/TRIPEND, append to local log
# ============================================================
def _append_trip_log(record):
    """Append one line of JSON to TRIPS_LOG_FILE.  Best-effort."""
    try:
        with open(TRIPS_LOG_FILE, "a") as f:
            f.write(json.dumps(record, separators=(',', ':')))
            f.write("\n")
    except Exception as e:
        print("[TRIP] log append failed:", e)


def _handle_trip_event(radio, ev):
    """Send a TRIPSTART or TRIPEND over LoRa and append to local log.
    ev is (kind, payload_dict) from TripTracker.update().
    """
    kind, payload = ev
    body = json.dumps(payload, separators=(',', ':'))
    line = kind + ":" + body
    if kind == "TRIPSTART":
        print("\n[TRIP START] device={} lat={} lon={}".format(
            payload.get("device"), payload.get("lat"), payload.get("lon")))
    elif kind == "TRIPEND":
        print("\n[TRIP END] type={} dist={:.2f}km dur={}s avg={}km/h max={}km/h".format(
            payload.get("type"), payload.get("km", 0),
            payload.get("dur", 0), payload.get("avg", 0),
            payload.get("max", 0)))
    # Persist locally regardless of TX outcome (audit trail)
    _append_trip_log({"kind": kind, "msg": payload})
    # Send over LoRa
    try:
        ok = radio.send(line)
        print("[TRIP] sent OK" if ok else "[TRIP] sent FAILED (timeout)")
    except Exception as e:
        print("[TRIP] send exception:", e)
    # After TRIPEND: announce we have new data to sync
    if kind == "TRIPEND":
        _sync_announce(radio)
        return True  # sync was announced
    return False


def _sync_announce(radio):
    """Send SYNC:<hwid> to tell Pi 5 we have unsent trip data.
    Called on TRIPEND and on boot if unsent trips exist.
    Wire prefix carries the permanent hwid so the Pi keys sessions
    by it (rename-proof). Bridge OLED resolves to friendly name.
    """
    msg = "SYNC:{}".format(DEVICE_HWID)
    try:
        ok = radio.send(msg)
        print("[SYNC] announced -> {}".format("OK" if ok else "FAIL"))
    except Exception as e:
        print("[SYNC] announce exception:", e)


# ---- Liveness roster -------------------------------------------------------
# hwid -> [name, last_seen_ticks_ms]. Every node re-announces DEVICE every
# DEVICE_ANNOUNCE_MS; receiving one is a heartbeat. PicoB is headless so this
# is kept for completeness (so the node "knows who's alive") — no display.
_alive_devices = {}


def _note_alive(hwid, name):
    if hwid:
        _alive_devices[hwid] = [name, time.ticks_ms()]


def _device_announce(radio):
    """Broadcast our DEVICE: announce — doubles as the liveness heartbeat.
    The Pi's DEVICE: ingest bumps devices.last_seen (chat presence dots);
    peers record us in their roster."""
    _note_alive(DEVICE_HWID, DEVICE_ID)
    try:
        radio.send('DEVICE:{"id":"' + DEVICE_HWID +
                   '","name":"' + DEVICE_ID + '"}')
    except Exception as e:
        print("[ALIVE] announce error:", e)


def _handle_device_message(radio, text):
    """Handle DEVICE:<json> umbrella for device-management.

    Wire grammar — every payload carries BOTH fields:
      id   = permanent hardware id (hex)             - immutable
      name = renameable label loaded from device_id.txt
    DEVICE:{"id":"<hwid>","name":"<current_or_new_name>"}

    Direction is implicit:
      announce (device->Pi):  id=mine, name=mine
      rename   (Pi->device):  id=mine, name=<new>     (name != current)
      ack post-reboot:        id=mine, name=<new>     (name now == current)

    A packet whose id matches us AND whose name differs from our current
    is a rename request — persist `name` + reboot. Other shapes (id not
    ours, or name matches our current) are silently consumed (echoes of
    other nodes' announces, our own boot announce coming back, etc.).

    Returns True if it was a DEVICE: payload (consumed, possibly
    rebooting); False otherwise.
    """
    if not text or not text.startswith("DEVICE:"):
        return False
    body = text[7:]
    try:
        obj = json.loads(body)
    except Exception:
        return True  # malformed — consume
    hwid = obj.get("id")
    name = obj.get("name")
    if not hwid or not name:
        return True  # missing required fields — ignore
    # Any DEVICE: announce is a liveness heartbeat — record the sender.
    if hwid != DEVICE_HWID:
        _note_alive(hwid, name)
    if hwid != DEVICE_HWID:
        return True  # not addressed to this hardware
    if name == DEVICE_ID:
        return True  # echo of our own announce, or rename to current name
    if len(name) >= 16:
        return True
    for c in name:
        if not (c.isalpha() or c.isdigit() or c in "_-"):
            return True

    print("[DEVICE] rename {} -> {}".format(DEVICE_ID, name))
    try:
        with open(DEVICE_ID_FILE, "w") as f:
            f.write(name)
    except Exception as e:
        print("[DEVICE] persist failed:", e)
        return True

    # Reboot — the fresh boot announce (with the new name) is the ack.
    time.sleep(0.5)
    try:
        import machine
        machine.reset()
    except Exception:
        pass
    return True


def _handle_who_msg(radio, text):
    """Handle 'WHO?' broadcast presence probes from Pi 5.

    Replies with the same DEVICE: announce we send on boot. The Pi's
    DEVICE: ingest path bumps devices.last_seen, which is what the chat
    presence dots read. Broadcast, so anyone hearing it answers — the
    natural overlap of replies is fine; the receiver dedups by hwid.

    Returns True if matched (consumes the packet).
    """
    if text != "WHO?" and not text.startswith("WHO?"):
        return False
    try:
        radio.send('DEVICE:{"id":"' + DEVICE_HWID +
                   '","name":"' + DEVICE_ID + '"}')
        print("[WHO] re-announced DEVICE")
    except Exception as e:
        print("[WHO] send error:", e)
    return True


def _handle_qpos_msg(radio, gps, text):
    """Handle 'QPOS:<target>' on-demand position queries from Pi 5.

    Replies by broadcasting the latest cached GPS fix exactly as if a
    normal cadence emission had fired. Receivers can't tell ping replies
    apart from periodic broadcasts — the live-points pipeline ingests
    both the same way.

    Returns True if the wire-format matched (so the caller consumes the
    packet whether or not we replied), False otherwise.
    """
    if not text.startswith("QPOS:"):
        return False
    target = text[5:].strip()
    if target != DEVICE_HWID and target != DEVICE_ID:
        return True  # somebody else's ping
    fix = gps.latest() if gps is not None else None
    if fix is None or fix.get("lat") is None:
        print("[QPOS] no fix yet, ignoring")
        return True
    msg = format_gps_from_fix(fix)
    try:
        ok = radio.send(msg)
        print("[QPOS] reply: {} (ok={})".format(msg, ok))
    except Exception as e:
        print("[QPOS] send error:", e)
    return True


def _handle_incoming_sync_msg(radio, text):
    """Handle Q/R messages received from Pi 5 via Pico A relay.

    Returns True if the message was a sync protocol message (handled here),
    False if it should be handled as a regular chat message.

    Pi 5 sends:
        QTRIPS:<hwid>                - list all trip IDs
        QTRIP:<trip_id>              - send metadata for one trip
        QPTS:<trip_id>:<from>:<count>- send fix batch
        ACK:<trip_id>                - trip confirmed received in DB

    Pico B replies:
        RTRIPS:<hwid>:<id1>,<id2>,...
        RTRIP:<trip_id>:<json_meta>
        RPTS:<trip_id>:<from_idx>:<encoded_fixes>

    Wire prefix carries the permanent hwid (phase 3) so renames never
    redirect the session. The renameable name still travels inside the
    JSON meta of RTRIP for backwards-compat display only.

    Returns (True, session_active) where session_active indicates whether
    a sync query was being actively handled (so caller can update state).
    """
    if text.startswith("QTRIPS:"):
        target = text[7:].strip()
        # Accept either our hwid (new) or our current name (legacy Pi
        # that hasn't been redeployed yet). Anything else isn't us.
        if target != DEVICE_HWID and target != DEVICE_ID:
            return True
        unsent = trip_storage.get_unsent_trips()
        # Cap RTRIPS at ~200 B plaintext so the AES-PKCS7-padded
        # ciphertext fits comfortably in one SX1278 frame (FIFO is
        # 256 B, packet header eats a few). Each trip entry is roughly
        # "T<10digits>:<npts>" ≈ 14-17 chars. Once the backlog grows
        # past ~12 unsent trips, sending the whole list in one packet
        # blew past the FIFO and the bridge received a 30-byte garbage
        # fragment that always failed AES decrypt — sync deadlocked.
        # Any leftover trips ride the next sync cycle (PicoB
        # re-announces SYNC every SYNC_RETRY_MS while unsent remain).
        RTRIPS_BUDGET = 200
        header = "RTRIPS:{}:".format(DEVICE_HWID)
        body_budget = RTRIPS_BUDGET - len(header)
        parts = []
        used = 0
        for tid in unsent:
            npts = trip_storage.trip_npts(tid)
            entry = "{}:{}".format(tid, npts)
            sep = 1 if parts else 0   # ',' between entries
            if used + sep + len(entry) > body_budget:
                break
            parts.append(entry)
            used += sep + len(entry)
        truncated = len(parts) < len(unsent)
        reply = header + ",".join(parts)
        try:
            radio.send(reply)
            print("[SYNC] RTRIPS sent ({}/{} trips, {} B): {}".format(
                len(parts), len(unsent), len(reply),
                reply if not truncated else reply[:80] + "..."))
        except Exception as e:
            print("[SYNC] RTRIPS send error:", e)
        return True

    if text.startswith("QTRIP:"):
        trip_id = text[6:].strip()
        meta = trip_storage.read_meta(trip_id)
        if meta is None:
            print("[SYNC] QTRIP: trip {} not found".format(trip_id))
            return True
        compact = {
            "d":    meta.get("device", DEVICE_ID),
            # Include the permanent hwid so Pi can route the RTRIP to
            # the right session even if this device's name has changed
            # since the trip was recorded.
            "hwid": meta.get("hwid", DEVICE_HWID),
            "type": meta.get("type", "unknown"),
            "sts":  meta.get("start_ts"),
            "ets":  meta.get("end_ts"),
            "slat": meta.get("start_lat"),
            "slon": meta.get("start_lon"),
            "elat": meta.get("end_lat"),
            "elon": meta.get("end_lon"),
            "km":   meta.get("km"),
            "dur":  meta.get("dur"),
            "avg":  meta.get("avg"),
            "max":  meta.get("max"),
        }
        body = json.dumps(compact, separators=(",", ":"))
        reply = "RTRIP:{}:{}".format(trip_id, body)
        try:
            radio.send(reply)
            print("[SYNC] RTRIP sent for {} ({} bytes)".format(
                trip_id, len(reply)))
        except Exception as e:
            print("[SYNC] RTRIP send error:", e)
        return True

    if text.startswith("QPTS:"):
        parts = text[5:].split(":", 2)
        if len(parts) != 3:
            return True
        trip_id, from_s, count_s = parts
        try:
            from_idx = int(from_s)
            count    = int(count_s)
        except ValueError:
            return True
        fixes = trip_storage.read_fixes_range(trip_id, from_idx, count)
        if not fixes:
            reply = "RPTS:{}:{}:[]".format(trip_id, from_idx)
            try:
                radio.send(reply)
            except Exception:
                pass
            return True
        encoded, n_packed = sync_codec.encode_rpts(fixes)
        reply = "RPTS:{}:{}:{}".format(trip_id, from_idx, encoded)
        try:
            ok = radio.send(reply)
            print("[SYNC] RPTS {} fixes from {} ok={}".format(
                n_packed, from_idx, ok))
        except Exception as e:
            print("[SYNC] RPTS send error:", e)
        if trip_storage.sync_status(trip_id) == trip_storage.SYNC_UNSENT:
            trip_storage.mark_sync_status(trip_id, trip_storage.SYNC_SENT)
        return True

    if text.startswith("ACK:"):
        trip_id = text[4:].strip()
        # Hub confirms it has the trip — delete the .gps + .json files
        # from flash and drop the sync_state entry. Keeping confirmed
        # trips around was costing real space (97 leftovers found in
        # the field) and gave the size-cap path more to scan.
        try:
            trip_storage.delete_trip(trip_id)
            trip_storage.clear_sync_status(trip_id)
        except Exception as e:
            print("[SYNC] ACK cleanup failed for {}: {}".format(trip_id, e))
        print("[SYNC] ACK received for {} - files deleted".format(trip_id))
        return True

    return False


# ============================================================
# OLD pinout - matches reference card / pico_lora_bridge.py
# ============================================================
LORA_SCK  = 2
LORA_MOSI = 3
LORA_MISO = 4
LORA_CS   = 5
LORA_RST  = 22
LORA_DIO0 = 26
ONBOARD_LED = 25


# ============================================================
# Registers / modes
# ============================================================
REG_FIFO             = 0x00
REG_OP_MODE          = 0x01
REG_FRF_MSB          = 0x06
REG_FRF_MID          = 0x07
REG_FRF_LSB          = 0x08
REG_PA_CONFIG        = 0x09
REG_LNA              = 0x0C
REG_FIFO_ADDR_PTR    = 0x0D
REG_FIFO_TX_BASE     = 0x0E
REG_FIFO_RX_BASE     = 0x0F
REG_FIFO_RX_CURRENT  = 0x10
REG_IRQ_FLAGS        = 0x12
REG_RX_NB_BYTES      = 0x13
REG_PKT_SNR_VALUE    = 0x19
REG_PKT_RSSI_VALUE   = 0x1A
REG_MODEM_CONFIG_1   = 0x1D
REG_MODEM_CONFIG_2   = 0x1E
REG_PREAMBLE_MSB     = 0x20
REG_PREAMBLE_LSB     = 0x21
REG_PAYLOAD_LENGTH   = 0x22
REG_MODEM_CONFIG_3   = 0x26
REG_SYNC_WORD        = 0x39
REG_VERSION          = 0x42

MODE_LORA            = 0x80
MODE_SLEEP           = 0x00
MODE_STDBY           = 0x01
MODE_TX              = 0x03
MODE_RX_CONTINUOUS   = 0x05

IRQ_TX_DONE          = 0x08
IRQ_RX_DONE          = 0x40
IRQ_PAYLOAD_CRC_ERR  = 0x20


# ============================================================
# Tiny driver - just enough to send
# ============================================================
class LoRaTx:
    def __init__(self):
        self.spi = SPI(0, baudrate=5_000_000,
                       sck=Pin(LORA_SCK),
                       mosi=Pin(LORA_MOSI),
                       miso=Pin(LORA_MISO))
        self.cs  = Pin(LORA_CS,  Pin.OUT, value=1)
        self.rst = Pin(LORA_RST, Pin.OUT, value=1)
        self.led = Pin(ONBOARD_LED, Pin.OUT, value=0)
        self.reset()
        self.init_radio()

    def reset(self):
        self.rst.value(0); time.sleep(0.1)
        self.rst.value(1); time.sleep(0.5)

    def read_reg(self, reg):
        self.cs.value(0)
        buf = bytearray(2)
        self.spi.write_readinto(bytes([reg & 0x7F, 0x00]), buf)
        self.cs.value(1)
        return buf[1]

    def write_reg(self, reg, value):
        self.cs.value(0)
        self.spi.write(bytes([reg | 0x80, value & 0xFF]))
        self.cs.value(1)

    def init_radio(self):
        v = self.read_reg(REG_VERSION)
        if v != 0x12:
            raise RuntimeError("SX1278 not found, version=0x%02x" % v)

        self.write_reg(REG_OP_MODE, MODE_LORA | MODE_SLEEP)
        time.sleep(0.01)
        self.write_reg(REG_OP_MODE, MODE_LORA | MODE_STDBY)
        time.sleep(0.01)

        # 433 MHz
        self.write_reg(REG_FRF_MSB, 0x6C)
        self.write_reg(REG_FRF_MID, 0x80)
        self.write_reg(REG_FRF_LSB, 0x00)

        # +20 dBm, max LNA
        self.write_reg(REG_PA_CONFIG,      0xFF)
        self.write_reg(REG_LNA,            0x23)

        # BW125, CR4/5, SF+CRC — MUST match lora.py on Pico A + ESP32-C3.
        # Change LORA_SF to tune range vs airtime:
        #   SF9  -> ~2x range vs SF7,  ~4x airtime
        #   SF10 -> ~3x range vs SF7,  ~8x airtime
        #   SF11 -> ~4x range vs SF7, ~16x airtime  (current — max range mode)
        LORA_SF = 9   # <-- change here + lora.py on Pico A + ESP32-C3 config.h
        # AGC auto (bit 2). LowDataRateOptimize (bit 3) MUST be set when
        # symbol time > 16 ms, i.e. SF11/SF12 at BW125 — datasheet §4.1.1.6.
        # Forgetting this bit causes random decode failures at high SF.
        ldro = 0x08 if LORA_SF >= 11 else 0x00
        self.write_reg(REG_MODEM_CONFIG_3, 0x04 | ldro)
        self.write_reg(REG_MODEM_CONFIG_1, 0x72)             # BW125 CR4/5
        self.write_reg(REG_MODEM_CONFIG_2, (LORA_SF << 4) | 0x04)   # SF+CRC

        # Preamble 8, sync 0x34
        self.write_reg(REG_PREAMBLE_MSB, 0x00)
        self.write_reg(REG_PREAMBLE_LSB, 0x08)
        self.write_reg(REG_SYNC_WORD,    0x34)

        # FIFO base
        self.write_reg(REG_FIFO_TX_BASE, 0x00)
        self.write_reg(REG_FIFO_RX_BASE, 0x00)

        print("Radio initialized (version=0x%02x, 433 MHz, SF%d, BW125, sync 0x34)" % (v, LORA_SF))

        # Start in RX mode so we can hear incoming packets
        self.rx_mode()

    def rx_mode(self):
        """Put radio into continuous receive mode."""
        self.write_reg(REG_IRQ_FLAGS, 0xFF)
        self.write_reg(REG_FIFO_ADDR_PTR, 0x00)
        self.write_reg(REG_OP_MODE, MODE_LORA | MODE_RX_CONTINUOUS)
        time.sleep(0.005)

    def poll_rx(self):
        """Returns (bytes, rssi_dbm, snr_db) or None if nothing received."""
        flags = self.read_reg(REG_IRQ_FLAGS)
        if not (flags & IRQ_RX_DONE):
            return None
        crc_err = bool(flags & IRQ_PAYLOAD_CRC_ERR)
        self.write_reg(REG_IRQ_FLAGS, 0xFF)
        if crc_err:
            return None

        n   = self.read_reg(REG_RX_NB_BYTES)
        cur = self.read_reg(REG_FIFO_RX_CURRENT)
        self.write_reg(REG_FIFO_ADDR_PTR, cur)
        data = bytearray(n)
        for i in range(n):
            data[i] = self.read_reg(REG_FIFO)

        rssi_raw = self.read_reg(REG_PKT_RSSI_VALUE)
        rssi_dbm = rssi_raw - 164
        snr_raw = self.read_reg(REG_PKT_SNR_VALUE)
        if snr_raw > 127: snr_raw -= 256
        snr_db = snr_raw / 4.0
        return bytes(data), rssi_dbm, snr_db

    def send(self, payload):
        # Encrypt - replaces the str->bytes step with encrypt() (which also
        # handles the str->bytes conversion internally)
        payload = encrypt(payload)
        # Fail loud rather than truncate. Truncated ciphertext is
        # indistinguishable from random bytes after AES-ECB decrypt
        # and produces 'decrypt_failed_showing_raw' on the bridge —
        # the root cause behind several lost-packet bugs this week.
        if len(payload) > 250:
            print("[RADIO] send refused: payload {} > 250 B".format(len(payload)))
            return False

        self.write_reg(REG_OP_MODE,  MODE_LORA | MODE_STDBY)
        self.write_reg(REG_IRQ_FLAGS, 0xFF)
        self.write_reg(REG_FIFO_ADDR_PTR, 0x00)
        for b in payload:
            self.write_reg(REG_FIFO, b)
        self.write_reg(REG_PAYLOAD_LENGTH, len(payload))

        self.led.value(1)
        self.write_reg(REG_OP_MODE, MODE_LORA | MODE_TX)

        # Wait for TxDone (max 3 s)
        sent = False
        t0 = time.ticks_ms()
        while time.ticks_diff(time.ticks_ms(), t0) < 3000:
            if self.read_reg(REG_IRQ_FLAGS) & IRQ_TX_DONE:
                sent = True
                break
            time.sleep_ms(2)

        self.write_reg(REG_IRQ_FLAGS, 0xFF)
        self.led.value(0)
        # Return to RX mode so we keep listening for incoming messages
        self.rx_mode()
        return sent


# ============================================================
# Chat loop
# ============================================================
def chat():
    try:
        radio = LoRaTx()
    except Exception as e:
        print("ERROR initializing radio:", e)
        print("  Check antenna is connected and pins match the OLD wiring:")
        print("  SCK=2, MOSI=3, MISO=4, NSS=5, RST=22")
        return

    # One-shot boot announce so the Pi 5 "+ Add device" listen window can
    # discover unknown devices and rename them via the existing RENAME
    # protocol. Best-effort; failure is non-fatal.
    try:
        radio.send('DEVICE:{"id":"' + DEVICE_HWID +
                   '","name":"' + DEVICE_ID + '"}')
    except Exception as e:
        print("[ANNOUNCE] send error:", e)

    # Trip storage: ensures trips/ folder, prunes oversized folder, auto-
    # closes any "in_progress" trip whose last fix is stale (>1h old).
    try:
        st = trip_storage.init()
        print("Trip storage: {} trips on flash, {} bytes; stale_closed={}".format(
            st.get("trips", 0), st.get("bytes", 0), st.get("stale_closed")))
    except Exception as e:
        print("Trip storage init failed (continuing):", e)

    # GPS module - opens UART0 on GPIO 0/1, 9600 baud, NEO-7M
    # If wiring is missing the UART still opens; poll() just returns None
    # forever until a fix arrives. Failure here shouldn't kill chat.
    try:
        gps = gps_module.Gps(send_interval_s=CADENCE_BY_CLASS["idle"])
        print("GPS initialized (UART0 GPIO0/1 9600). Waiting for fix...")
    except Exception as e:
        print("GPS init failed (continuing without GPS):", e)
        gps = None

    # Trip tracker - state machine that watches GPS fixes and emits
    # TRIPSTART / TRIPEND events.  Pure logic, no hardware.
    tracker = trip_tracker.TripTracker(device_id=DEVICE_ID,
                                       device_hwid=DEVICE_HWID)

    print()
    print("LoRa Chat Ready  (device_id={})".format(DEVICE_ID))
    print("Type message + Enter to send. Type 'q' to quit.")
    print("Incoming messages will appear automatically.")
    print("GPS auto-broadcasts every 30s once a fix is acquired.")
    print("Trips auto-detected (start/end sent over LoRa).")
    print()

    counter = 0
    rx_counter = 0
    line_buf = ""
    # Boot resume: set True after we've made the resume decision (once per
    # boot, on the first GPS fix that has a UTC timestamp).
    _resumed_check_done = False
    # Last selected movement class - used for hysteresis in pick_class().
    _last_class = "idle"
    # Smoothed-speed window for cadence selection.  We feed the avg of last
    # N fix speeds into pick_class() instead of instantaneous spd, so a
    # single noise spike doesn't flip cadence.
    SPEED_SMOOTH_N = 5
    _recent_speeds = []
    # ---- Watchdog state ----
    _boot_ticks_ms     = time.ticks_ms()
    _last_fix_ticks_ms = None
    WATCHDOG_NO_FIX_MS = 5 * 60 * 1000     # 5 minutes
    # ---- Sync retry state ----
    # Resend SYNC:B1 every SYNC_RETRY_MS while unsent trips exist and no
    # active sync session is running.  This handles the case where Pi 5
    # received the first SYNC but the QTRIPS response never reached Pico B
    # (weak signal when walking away).  On next retry, signal may be better.
    SYNC_RETRY_MS        = 5 * 60 * 1000   # 5 min between retries
    _last_sync_ticks_ms  = 0               # ticks_ms when last SYNC was sent
    _sync_session_active = False            # True while answering Q* queries
    # ---- Liveness heartbeat state ----
    DEVICE_ANNOUNCE_MS    = 60 * 1000      # re-announce DEVICE every 60s
    _last_announce_ticks_ms = 0

    if not STANDALONE:
        poller = select.poll()
        poller.register(sys.stdin, select.POLLIN)
        sys.stdout.write("> ")
    else:
        poller = None
        print("(standalone mode - no keyboard input, no prompt)")

    # Announce on boot if there are unsent trips from a previous session.
    try:
        unsent = trip_storage.get_unsent_trips()
        if unsent:
            print("[SYNC] {} unsent trip(s) on boot: {}".format(
                len(unsent), unsent))
            _sync_announce(radio)
            _last_sync_ticks_ms = time.ticks_ms()
    except Exception as e:
        print("[SYNC] boot check error:", e)

    while True:
        now = time.ticks_ms()

        # ---- Check for incoming LoRa packets ----
        got = radio.poll_rx()
        if got is not None:
            data, rssi, snr = got
            decrypted = decrypt(data)
            if decrypted is None:
                msg_text = "[decrypt failed: %s]" % data.hex()
            else:
                try:
                    msg_text = decrypted.decode("utf-8")
                except UnicodeError:
                    msg_text = decrypted.hex()
            rx_counter += 1
            # DEVICE: first — rename actions reboot and never return; the
            # echo of our own boot 'hello' is silently consumed too.
            if decrypted is not None and _handle_device_message(radio, msg_text):
                pass  # unreachable for rename; consumed for hello/etc.
            # QPOS: ahead of the sync dispatcher so it doesn't flip
            # _sync_session_active — it's a single fire-and-forget request,
            # not part of the Q/R sync flow.
            elif decrypted is not None and _handle_qpos_msg(radio, gps, msg_text):
                pass
            elif decrypted is not None and _handle_who_msg(radio, msg_text):
                pass
            # Then check if this is a sync protocol message
            elif decrypted is not None and _handle_incoming_sync_msg(radio, msg_text):
                # A Q* message means Pi 5 has an active sync session
                _sync_session_active = True
                # Reset retry timer — Pi 5 is clearly alive and querying
                _last_sync_ticks_ms = now
                # ACK means session is complete
                if msg_text.startswith("ACK:"):
                    _sync_session_active = False
            else:
                print("\n[RX %d] %s  (rssi=%d snr=%.1f)" % (
                    rx_counter, msg_text, rssi, snr))
                if not STANDALONE:
                    sys.stdout.write("> " + line_buf)

        # ---- Sync retry: re-announce if unsent trips and no active session ----
        # Handles the case where Pi 5 received SYNC but QTRIPS never reached
        # Pico B (weak signal).  After SYNC_RETRY_MS, try again.
        if (not _sync_session_active
                and not tracker.in_trip()
                and time.ticks_diff(now, _last_sync_ticks_ms) >= SYNC_RETRY_MS):
            try:
                unsent = trip_storage.get_unsent_trips()
                if unsent:
                    print("[SYNC] Retry: {} unsent trip(s)".format(len(unsent)))
                    _sync_announce(radio)
                    _last_sync_ticks_ms = now
            except Exception as e:
                print("[SYNC] retry error:", e)

        # ---- Liveness heartbeat: re-announce DEVICE every 60s ----
        # Skipped during an active sync session so the tiny announce TX
        # can't collide with Q/R reply traffic (same guard as GPS).
        if (not _sync_session_active
                and time.ticks_diff(now, _last_announce_ticks_ms) >= DEVICE_ANNOUNCE_MS):
            _device_announce(radio)
            _last_announce_ticks_ms = now

        # ---- Watchdog: force-close trip if no fix received for too long ----
        # Two scenarios this protects against:
        #   (A) GPS lost mid-trip (e.g. walked indoors, no sky view)
        #   (B) Boot-time stale in_progress with no GPS lock
        # Without this, the tracker would sit in MOVING forever waiting for
        # a stop-detect that can't fire (no fixes => no stationary detection).
        if tracker.in_trip():
            ref_ticks = _last_fix_ticks_ms if _last_fix_ticks_ms is not None else _boot_ticks_ms
            if time.ticks_diff(now, ref_ticks) >= WATCHDOG_NO_FIX_MS:
                ev = tracker.force_close(reason="watchdog_no_fix")
                if ev is not None:
                    print("\n[WATCHDOG] No fix for {}s while MOVING - force-closing trip {}".format(
                        WATCHDOG_NO_FIX_MS // 1000, ev[1].get("id")))
                    _handle_trip_event(radio, ev)
                    _last_sync_ticks_ms = now
                    _sync_session_active = False
                    # Reset the tracker for fresh GPS lock to start a new trip
                    _resumed_check_done = True
                    _last_class = "idle"
                    _recent_speeds = []

        # ---- GPS: drain UART, send a fix every send_interval_s seconds ----
        if gps is not None:
            fix = gps.poll()
            if fix is not None:
                _last_fix_ticks_ms = time.ticks_ms()    # reset watchdog
                # Don't broadcast GPS over LoRa while a sync session is
                # active — the radio is busy answering Q* queries and a
                # simultaneous TX would cause a collision.  The fix is
                # still fed into the tracker and saved to disk; we just
                # skip the LoRa broadcast this once.
                if not _sync_session_active:
                    msg = format_gps_from_fix(fix)
                    ok = radio.send(msg)
                    if ok:
                        print("\n[GPS TX] %s" % msg)
                    else:
                        print("\n[GPS TX FAILED - timeout] %s" % msg)
                else:
                    print("\n[GPS TX skipped - sync active]")
                # Resume check: on first fix with GPS-UTC time, decide
                # whether any in_progress trip from a previous boot should
                # be resumed or closed.
                if not _resumed_check_done and gps.latest_gps_ts() is not None:
                    res = tracker.boot_resume_check(fix)
                    _resumed_check_done = True
                    if res is not None:
                        kind, tid = res
                        print("\n[RESUME] {} trip_id={}".format(kind, tid))
                # Feed the same fix into the trip tracker.  Returns a LIST
                # of events (possibly multiple if a SPLIT happened mid-trip).
                evs = tracker.update(fix)
                for ev in evs:
                    sync_sent = _handle_trip_event(radio, ev)
                    if sync_sent:
                        _last_sync_ticks_ms = now
                        _sync_session_active = False
                # Adjust GPS cadence based on movement class:
                #   IDLE                  -> 60s   (no trip in progress)
                #   walking (< ~7 km/h)   -> 15s
                #   cycling (~7-25 km/h)  -> 10s
                #   auto    (>= ~25 km/h) -> 10s
                # Use SMOOTHED speed (avg of last 5 fixes) so a single
                # noisy speed spike doesn't flip cadence.  Hysteresis in
                # pick_class still further dampens border-line transitions.
                if tracker.in_trip():
                    spd = fix.get("spd", 0.0) or 0.0
                    _recent_speeds.append(spd)
                    if len(_recent_speeds) > SPEED_SMOOTH_N:
                        _recent_speeds.pop(0)
                    smoothed = sum(_recent_speeds) / len(_recent_speeds)
                    new_class = pick_class(smoothed, _last_class)
                    if new_class != _last_class:
                        _last_class = new_class
                    gps.set_interval(CADENCE_BY_CLASS[new_class])
                else:
                    _last_class = "idle"
                    _recent_speeds = []
                    gps.set_interval(CADENCE_BY_CLASS["idle"])
                # Redraw prompt + in-progress line so user's typing isn't lost
                if not STANDALONE:
                    sys.stdout.write("> " + line_buf)

        # ---- Poll for typed characters (non-blocking, 50ms) ----
        if STANDALONE:
            # No keyboard - just pace the loop so we don't peg the CPU.
            time.sleep_ms(50)
            continue

        if poller.poll(50):
            try:
                ch = sys.stdin.read(1)
            except Exception:
                ch = None
            if ch is None:
                continue
            if ch == "\n" or ch == "\r":
                msg = line_buf.strip()
                line_buf = ""
                print()  # newline after the user's input
                if msg.lower() in ("q", "quit", "exit"):
                    print("Bye.")
                    return
                # GPS shortcut: "gps 50.07 14.43 [alt] [spd]" -> formatted JSON
                if msg.lower().startswith("gps ") or msg.lower() == "gps":
                    parts = msg.split()[1:]  # drop the "gps" keyword
                    try:
                        msg = format_gps_message(parts)
                        print("(GPS formatted: %s)" % msg)
                    except ValueError as e:
                        print("[ERROR] %s" % e)
                        sys.stdout.write("> ")
                        continue
                    except Exception as e:
                        print("[ERROR] could not format GPS: %s" % e)
                        sys.stdout.write("> ")
                        continue
                if msg:
                    counter += 1
                    ok = radio.send(msg)
                    if ok:
                        print("[TX %d] %s" % (counter, msg))
                    else:
                        print("[TX %d FAILED - timeout] %s" % (counter, msg))
                sys.stdout.write("> ")
            elif ch == "\x7f" or ch == "\x08":  # backspace
                if line_buf:
                    line_buf = line_buf[:-1]
                    sys.stdout.write("\x08 \x08")
            elif ch == "\x03":  # Ctrl-C
                print("\nBye.")
                return
            else:
                line_buf += ch
                sys.stdout.write(ch)


# Run chat() only when this file is executed directly (Thonny "Run").
# When imported from main.py for standalone boot, main.py decides when
# to start chat() and may set STANDALONE = True first.
if __name__ == "__main__":
    chat()