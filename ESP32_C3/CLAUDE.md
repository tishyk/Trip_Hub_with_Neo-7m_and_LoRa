# ESP32_C3 — ESP32-C3 supermini LoRa+GPS+chat node

## Role
Replacement for the Pico B tracker, plus a new feature on top: hosts its own WiFi softAP + web chat UI so a phone can chat over LoRa with no Pi 5 in the loop. Long-term: also does GPS broadcast + trip detection + sync, identical wire protocol to PicoB.

## Language — fixed
**Arduino C++ via PlatformIO**, board `esp32-c3-devkitm-1`, framework `arduino`, filesystem LittleFS. Libraries: `RadioLib` (LoRa) + `TinyGPSPlus` (NMEA). Do not switch to ESP-IDF or MicroPython here. Do not copy the `.py` files from [../PicoB/](../PicoB/) — those are spec, not source.

## Where to tune values
[include/config.h](include/config.h) is the single inventory of
field-tunable constants on the C3: pin maps (`pins::`), LoRa air
params (`rf::`), AES key (`crypto_cfg::AES_KEY`), GPS cadence by
class with hysteresis (`gps_cfg::`), trip-tracker thresholds
(`trip_cfg::`), and softAP wifi (`wifi::`). [src/crypto.cpp](src/crypto.cpp)
imports the key from `crypto_cfg` so the value lives in one place
across the C3 and is greppable from PicoB's [config.py](../PicoB/config.py).

## Pin map ([include/config.h](include/config.h))
| Function | GPIO |
|---|---|
| LoRa SCK / MISO / MOSI / NSS / RST / DIO0 | 4 / 5 / 6 / 7 / 10 / 3 |
| GPS RX / TX (UART to NEO-7M) | 20 / 21 |

## Network parameters — must match Hub_Server_Firmware + PicoB exactly
- **Carrier 434.0 MHz** (not 433 — legacy `0x6C8000` decodes to 434.0 exactly).
- BW 125 kHz, SF 9, CR 4/5, sync 0x34, preamble 8, CRC on, +20 dBm via PA_BOOST.
- AES-128-ECB, key = ASCII `"LoRaMeshDemoKey1"`, PKCS7 padding, max 250 B post-encryption.

## WiFi softAP
- SSID `LoraWan`, password `ChangeMe-LoRa24`, channel 6, max 2 clients.
- HTTP UI on `192.168.4.1`. Plain `WebServer.h` + `WiFi.h` — no AsyncWebServer.
- Polling `/api/messages?since=N` every 3 s. Newest message at bottom (auto-scrolls if the user is near the bottom; preserves position when scrolled up).
- Persistent: ring of last 50 messages mirrored to LittleFS, rewritten every 25 changes (flash-wear).
- **Send path**: web UI prepends `CHAT:` before encrypt+TX. Live byte counter caps body at 220 B so the AES-padded packet stays under the 250 B LoRa cap. UI rejects overflow with HTTP 413.
- **Receive path**: only `CHAT:`-tagged packets are added to `Chat`. Other prefixes (GPS / TRIP* / SYNC / Q* / R* / ACK) are logged on serial only — the chat UI never sees protocol traffic.

## Current source layout ([src/](src/), [include/](include/))
| File | Status |
|---|---|
| [main.cpp](src/main.cpp) | Brings up Chat → Net → Web → Gps → Radio. Loop polls RX, prints heartbeat. |
| [lora_radio.cpp](src/lora_radio.cpp) | RadioLib SX1278, AES via [crypto.cpp](src/crypto.cpp). Done. |
| [crypto.cpp](src/crypto.cpp) | AES-128-ECB + PKCS7. Done. |
| [net.cpp](src/net.cpp) | softAP. Done. |
| [web_server.cpp](src/web_server.cpp) | HTTP chat UI. Done. |
| [chat_log.cpp](src/chat_log.cpp) | RAM ring + LittleFS mirror. Done. |
| [gps.cpp](src/gps.cpp) | Currently `pumpToSerial()` only — no parsing into trip pipeline yet. |

## What's still missing vs. PicoB
1. NMEA → fix struct (lat, lon, alt, speed, ts) using TinyGPSPlus.
2. Cadence selector with hysteresis (port from [../PicoB/runtime.py](../PicoB/runtime.py)).
3. Trip tracker IDLE↔MOVING state machine (port from [../PicoB/trip_tracker.py](../PicoB/trip_tracker.py)).
4. Trip storage on LittleFS (`/trips/T<ts>.gps` + `.json`, plus `in_progress.txt`, `sync_state.json`).
5. Sync handshake: send `SYNC:<DEVICE_ID>` on boot/after-trip-end/5-min-retry; respond to `QTRIPS`/`QTRIP`/`QPTS`; mark CONFIRMED on `ACK`.
6. Periodic `GPS:{json}` broadcast at the class-selected cadence.

## DEVICE_ID
Defaults to `"SergiiT"` ([config.h:5](include/config.h#L5)). Override at compile time via `platformio.ini` `build_flags = -DDEVICE_ID=\"C1\"` if needed for multi-device tests.

## Known traps
- `transmit()` on RadioLib leaves the chip in standby — must call `startReceive()` again afterward (already done in [lora_radio.cpp:47](src/lora_radio.cpp#L47)).
- ESP32-C3 has no IRAM_ATTR-required *for* most ISRs but RadioLib's `setPacketReceivedAction` callback is registered as `IRAM_ATTR onRxIsr` — keep it tiny (just a flag).
- Ra-01 draws ~120 mA on TX. If C3 resets during transmit, decouple with 100 nF + 100 µF across VCC/GND with short legs.
