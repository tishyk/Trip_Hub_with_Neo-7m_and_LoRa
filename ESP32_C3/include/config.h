#pragma once
#include <stddef.h>
#include <stdint.h>

#ifndef DEVICE_ID
#define DEVICE_ID "esp32-c3"
#endif

// Runtime-mutable device id. Initialised to the compile-time DEVICE_ID at
// startup, then overridden from /device_id.txt if a rename has been
// persisted. All runtime LoRa / web / sync code should reference
// g_deviceId, not DEVICE_ID directly — DEVICE_ID is the boot-time fallback.
extern char g_deviceId[16];

// Permanent hardware id, hex-encoded ESP32 chip MAC. Populated in
// computeHwid() during setup(). Other translation units (sync_manager
// etc.) read this when constructing wire payloads.
extern char g_deviceHwid[16];

namespace pins {
constexpr int LORA_SCK  = 4;
constexpr int LORA_MISO = 5;
constexpr int LORA_MOSI = 6;
constexpr int LORA_NSS  = 7;
constexpr int LORA_RST  = 10;
constexpr int LORA_DIO0 = 3;
constexpr int GPS_RX    = 20;
constexpr int GPS_TX    = 21;
}

namespace rf {
// 434.0, not 433 -- legacy Pico lora.py hard-codes FRF=0x6C8000
// which decodes to 434.0 MHz exactly. The "433 MHz" label there
// is loose terminology; both ends must match the actual RF carrier.
constexpr float   FREQ_MHZ = 434.0f;
constexpr float   BW_KHZ   = 125.0f;
// SF must match Pico A (Hub_Server_Firmware/lora.py) and Pico B
// (PicoB/runtime.py) exactly. Currently SF9 — chosen for sync speed
// while a large backlog drains; bump to SF11 for max range when sync
// is quiet. RadioLib handles LDRO automatically.
constexpr uint8_t SF       = 9;
constexpr uint8_t CR       = 5;
constexpr uint8_t SYNC     = 0x34;
constexpr uint8_t PREAMBLE = 8;
constexpr int8_t  POWER    = 20;
}

// AES-128-ECB key shared with PicoB (config.py:LORA_KEY) and Pico A.
// Must match exactly across every node.
namespace crypto_cfg {
constexpr uint8_t AES_KEY[16] =
    {'L','o','R','a','M','e','s','h','D','e','m','o','K','e','y','1'};
constexpr size_t  BLOCK = 16;
}

constexpr uint32_t TX_PERIOD_MS = 5000;

namespace wifi {
constexpr const char* SSID        = "LoraWan";
constexpr const char* PASSWORD    = "ChangeMe-LoRa24";
constexpr int         CHANNEL     = 6;
constexpr int         MAX_CLIENTS = 2;
}

// ----- GPS cadence + class hysteresis (match PicoB's runtime.py) ------
namespace gps_cfg {
constexpr uint32_t IDLE_INTERVAL_MS    = 60000;
constexpr uint32_t WALKING_INTERVAL_MS = 15000;
constexpr uint32_t CYCLING_INTERVAL_MS = 10000;
constexpr uint32_t DRIVING_INTERVAL_MS     = 10000;

constexpr float WALK_TO_CYCLE_UP   = 8.0f;
constexpr float CYCLE_TO_WALK_DOWN = 6.0f;
constexpr float CYCLE_TO_DRIVING_UP    = 27.0f;
constexpr float DRIVING_TO_CYCLE_DOWN  = 23.0f;

constexpr size_t SPEED_SMOOTH_N = 5;   // smoothed speed window
}

// ----- Trip tracker thresholds (match PicoB's trip_tracker.py) -----
namespace trip_cfg {
constexpr float    MOVE_SPEED_KMH = 5.0f;
constexpr uint32_t MOVE_HOLD_S    = 30;
constexpr float    START_DIST_M   = 100.0f;
constexpr float    END_DIST_M     = 15.0f;
// Number of accepted fixes the tracker has to see since boot before it
// will believe a trip-start trigger. NEO-7M wanders 50-200 m and
// reports phantom 1-5 km/h Doppler speed during the first few fixes
// after a cold lock; without this gate that drift fires `far` or
// `sustained` and produces a 6 min ghost walking trip on a desk.
constexpr uint32_t MIN_FIXES_BEFORE_START = 5;
// Retroactive trip-start: ring buffer of the most recent IDLE fixes
// that showed motion (spd >= TRIP_LOW_SPEED_KMH). When the trigger
// finally fires, these fixes become the trip's prefix so the start
// coord matches actual departure rather than the trigger moment.
constexpr float    TRIP_LOW_SPEED_KMH = 2.0f;
constexpr size_t   MOTION_BUFFER_MAX  = 6;   // ~60 s at 10 s cadence
// Post-trip sanity check: a trip that ends without confirming the
// start trigger conditions (no distance + no sustained speed) is
// almost certainly a false-start. Discard from flash + skip persist
// + broadcast. Thresholds mirror PicoB's trip_tracker.py constants.
constexpr float    MIN_REAL_TRIP_M       = 100.0f;
constexpr float    MIN_REAL_TRIP_MAX_KMH = 5.0f;

constexpr uint32_t STOP_S_WALKING = 120;
constexpr uint32_t STOP_S_CYCLING =  60;
constexpr uint32_t STOP_S_DRIVING     = 300;

constexpr float CLASS_WALK_MAX = 7.0f;
constexpr float CLASS_BIKE_MAX = 25.0f;
}
