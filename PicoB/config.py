"""
config.py - Single source of truth for PicoB device tunables.

Centralises every value you might want to change in the field without
hunting through the codebase. Modules can `from config import X` so
edits here propagate; for safety each consumer keeps a local fallback
in case config.py is missing or fails to import.

PicoB is in-production code, so this file is being introduced
incrementally — only modules explicitly migrated currently read from
here. Today: trip_storage.py. Other modules still hold their own
copies; the values below are the canonical reference, and once a
module migrates, both copies must stay in sync until the migration is
complete.

Sections (kept short on purpose):

    1. Device identity
    2. LoRa air parameters + AES key
    3. LoRa SX1278 pin map (battery-powered Pico B wiring)
    4. GPS NEO-7M UART
    5. GPS jump-rejection (gps_module.py)
    6. Cadence-by-class + class hysteresis
    7. Trip tracker thresholds
    8. Trip storage paths + buffering
    9. Sync protocol limits + retries
"""

# ---- 1. Device identity --------------------------------------------------
# Live identity is NOT this constant. runtime.py reads device_id.txt on
# flash (renameable over LoRa via the DEVICE: protocol) and derives the
# permanent hardware id from machine.unique_id(). This entry is kept as
# a documentation hook only; nothing imports DEVICE_ID from config.
# Current name on this device (as of last rename): "Picos-B1".
DEVICE_ID = None  # unused — see comment above

# ---- 2. LoRa air parameters ----------------------------------------------
# MUST match every node on the network: Hub_Server_Firmware (Pico A),
# ESP32_C3, and any future device. Carrier 434.0 (not 433) — see
# project_hardware memory note: 0x6C8000 in the FRF registers decodes to
# 434.0 MHz exactly.
LORA_FREQ_MHZ          = 434.0
LORA_BANDWIDTH_KHZ     = 125.0
LORA_SPREADING_FACTOR  = 9    # sync-speed mode; see runtime.py for live value
LORA_CODING_RATE       = 5            # CR 4/5
LORA_SYNC_WORD         = 0x34
LORA_PREAMBLE          = 8
LORA_CRC               = True
LORA_TX_POWER_DBM      = 20

# Application-layer encryption (AES-128-ECB + PKCS7). Same key on every node.
LORA_KEY               = b"LoRaMeshDemoKey1"   # exactly 16 bytes
LORA_AES_BLOCK         = 16

# ---- 3. LoRa SX1278 pin map (Pico B "old pinout", matches runtime.py) ---
LORA_SCK    = 2
LORA_MOSI   = 3
LORA_MISO   = 4
LORA_CS     = 5
LORA_RST    = 22
LORA_DIO0   = 26
ONBOARD_LED = 25

# ---- 4. GPS NEO-7M UART --------------------------------------------------
GPS_UART_ID = 0
GPS_BAUD    = 9600
GPS_TX_PIN  = 0    # Pico TX → GPS RX
GPS_RX_PIN  = 1    # Pico RX ← GPS TX

# ---- 5. GPS jump-rejection (gps_module.py) -------------------------------
MIN_DELTA_FLOOR_M       = 10.0     # threshold floor when median-delta is small
DELTA_NOISE_M           = 20.0     # GPS noise tolerance added to threshold
DELTA_HISTORY_LEN       = 5        # number of recent deltas to median over
MAX_CONSECUTIVE_REJECTS = 3        # force-accept after this many rejections

# ---- 6. Cadence by class + class hysteresis ------------------------------
# Seconds between GPS broadcasts. Faster cadence = denser trip line on
# the map, more battery + flash. IDLE is much slower because there's
# nothing useful to record.
CADENCE_BY_CLASS = {
    "idle":    60,
    "walking": 15,
    "cycling": 10,
    "driving": 10,
}

# Hysteresis bands (km/h). Once classified, only step UP to a higher
# class above upper_band, step DOWN below lower_band. Prevents
# bouncing across boundaries.
WALK_TO_CYCLE_UP   = 8.0
CYCLE_TO_WALK_DOWN = 6.0
CYCLE_TO_DRIVING_UP   = 27.0
DRIVING_TO_CYCLE_DOWN = 23.0

# Smoothed-speed window (number of fixes) fed into pick_class().
SPEED_SMOOTH_N = 5

# ---- 7. Trip tracker thresholds (trip_tracker.py) ------------------------
MOVE_SPEED_KMH = 5.0    # need this much speed to be considered moving
MOVE_HOLD_S    = 30     # ...sustained for this long to trigger TRIPSTART
START_DIST_M   = 100.0  # alternative trigger: jumped this far from anchor
END_DIST_M     = 15.0   # within this radius of stop anchor = stationary

# Stop-detect threshold (seconds) by peak class of the trip so far.
SPLIT_STOP_BY_CLASS = {
    "walking": 120,
    "cycling":  60,
    "driving": 300,
}

# Rolling window for trip's peak class (running max over fixes).
PEAK_WINDOW_S = 5 * 60

# Movement-class boundaries (km/h).
CLASS_WALK_MAX_KMH = 7.0
CLASS_BIKE_MAX_KMH = 25.0

# ---- 8. Trip storage (trip_storage.py) -----------------------------------
TRIPS_DIR        = "trips"
IN_PROGRESS_FILE = "in_progress.txt"
SYNC_STATE_FILE  = "sync_state.json"
TRIPS_LOG_FILE   = "trips_log.json"

FLUSH_EVERY      = 5                  # buffered fixes per flash write
SIZE_CAP_BYTES   = 1 * 1024 * 1024    # trips/ folder cap
STALE_SECONDS    = 60 * 60            # auto-close stale in_progress on boot

# ---- 9. Sync protocol ----------------------------------------------------
SYNC_MAX_PACKET_BYTES = 200          # RPTS plaintext cap before AES padding
SYNC_RETRY_MS         = 5 * 60 * 1000  # re-announce SYNC every 5 min
WATCHDOG_NO_FIX_MS    = 5 * 60 * 1000  # force-close trip after no fix this long
