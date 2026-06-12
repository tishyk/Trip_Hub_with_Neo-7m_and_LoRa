# Hub_Server_Firmware — Pico A LoRa↔serial bridge (MicroPython)

Firmware for the Pi Pico (RP2040) attached to the Pi 5 via USB. Acts
as the network's only LoRa radio, plus runs the local UI (OLED, LEDs,
buzzer, RTC, buttons, HC-SR04). The Pi 5 side is in
[../Hub_Server/](../Hub_Server/); the two halves form the network hub.

## Language — fixed
**MicroPython** (RP2040 build). Don't suggest C++/Arduino here.
Standalone — flashes to Pico flash, runs without a host attached;
when the host is present the USB serial channel is used.

## Pin map ([config.py](config.py))
| Function | GPIO |
|---|---|
| OLED I2C SDA/SCL | 0 / 1 |
| DS1302 CLK/DAT/RST | 2 / 3 / 4 |
| Buzzer | 5 |
| LEDs (G/B/R \| R/B/G) | 6,7,8 / 9,10,11 |
| Buttons L/R | 12 / 13 |
| HC-SR04 TRIG/ECHO | 14 / 15 |
| LoRa MISO/CS/SCK/MOSI/RST/DIO0 | 16 / 17 / 18 / 19 / 20 / 21 |

Older Pico-only LoRa wiring + 2-day SPI debug log lives in
[SETUP_SUMMARY.md](SETUP_SUMMARY.md) and
[SX1278_RA01_EXACT_PINOUT.md](SX1278_RA01_EXACT_PINOUT.md). They cover
the *original* Pico↔Pico bring-up before the Pi 5 was added — the
real production pinout is the table above.

## USB-serial command grammar (Pi 5 → Pico A)
- `TX:<plaintext>` → AES-encrypt + transmit, reply `OK` or `ERR:tx_*`
- `PING` → `PONG`
- `RESET` → re-init radio, reply `READY`
- `TIME:<iso>` → set DS1302, reply `OK` or `ERR:time:*`

## USB-serial events (Pico A → Pi 5)
- `READY`, `RX:<text>|<rssi>|<snr>`, `LOG:<note>`, `ERR:<reason>`

## Bridge routing for incoming LoRa traffic ([lora_bridge.py](lora_bridge.py))
- `GPS:` → 5 m dedup, blue blink, forward
- `TRIPSTART:` / `TRIPEND:` → forward, no OLED
- `SYNC:` / `RTRIPS:` / `RTRIP:` / `RPTS:` → transparent forward to Pi
- `QTRIPS:` / `QTRIP:` / `QPTS:` / `ACK:` (from Pi) → encrypt & relay over LoRa
- `CHAT:<text>` → strip prefix, OLED scroll, buzzer, history, forward
  bare text to Pi as `RX:<text>|<rssi>|<snr>`, auto-reply `CHAT:PONG <time>`
  to incoming `CHAT:PING`
- anything untagged → log + `LOG:rx_untagged` to Pi, **no** OLED/chat side-effects

## Modules

| File | Role |
|---|---|
| [main.py](main.py) | Boot, wires up modules, runs the main loop. |
| [lora.py](lora.py) | Raw SX1278 driver (SPI register pokes). |
| [lora_bridge.py](lora_bridge.py) | LoRa↔serial routing + dedup. |
| [lora_chat.py](lora_chat.py) | Standalone Pico-A chat tester (not loaded by main). |
| [serial_io.py](serial_io.py) | USB serial parser/dispatcher. |
| [ui.py](ui.py), [display.py](display.py), [leds.py](leds.py), [buzzer is in main.py](main.py), [buttons.py](buttons.py), [distance.py](distance.py) | OLED + LEDs + buttons + HC-SR04 input. |
| [clock_rtc.py](clock_rtc.py), [time_sync.py](time_sync.py) | DS1302 RTC. |
| [storage.py](storage.py), [messages.py](messages.py) | Chat history persistence on flash. |
| [hardware_tests.py](hardware_tests.py) | Bring-up self-test routines. |
| [config.py](config.py) | Pin map, AES key, LoRa air params. |

## Network parameters — must match across all nodes
- Carrier 434.0 MHz, BW 125 kHz, SF 9, CR 4/5, sync 0x34, preamble 8, CRC on
- AES-128-ECB, key `LoRaMeshDemoKey1` (PKCS7 padded, max 250 B post-encryption)

## Known traps
- All SPI ops on the SX1278 driver need the `time.sleep_us(50)` delays —
  see [lora.py:89](lora.py#L89). Removing them breaks reads silently.
- MicroPython `Timer` objects must be kept on a module-level reference
  or GC frees them mid-countdown. See [main.py:22](main.py#L22).
- `lora_chat.py` is a standalone tester — not loaded by `main.py`. Its
  AES key was historically wrong; now fixed to `LoRaMeshDemoKey1`.

## Status
Proven, in-production code. The Pi 5 ↔ Pico A USB protocol is
documented from the host side in
[../Hub_Server/CLAUDE.md](../Hub_Server/CLAUDE.md).
