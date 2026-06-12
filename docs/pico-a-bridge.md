# Pico A — LoRa ↔ Pi bridge + hub UI

The fleet’s only radio attached to the gateway. It’s a transparent, encrypting
modem between LoRa and the Pi’s USB serial, and it drives a local OLED/LED/buzzer
status UI.

| | |
|---|---|
| **Folder** | [../Hub_Server_Firmware/](../Hub_Server_Firmware/) |
| **MCU** | Raspberry Pi Pico (RP2040) |
| **Runtime** | MicroPython |
| **Radio** | SX1278 (bespoke register driver) |
| **Peripherals** | SSD1306 OLED, LEDs, buzzer, DS1302 RTC, buttons, HC-SR04 |
| **Pinout** | [hardware.md → Pico A](hardware.md#pico-a--bridge--hub-hub_server_firmwareconfigpy) |
| **Case** | 3D-printed (Fusion 360) — STL on Printables, _coming soon_ |

![Pico A bridge — 3D-printed case (HC-SR04 "eyes", proximity-wake OLED clock)](assets/case-pico-bridge.jpg)
![Pico A bridge — RGB status LEDs running, OLED message](assets/case-pico-bridge-leds.jpg)

## What it does

- **Bridge** — decrypts incoming LoRa frames and forwards them to the Pi as
  `RX:<text>|<rssi>|<snr>`; encrypts `TX:<text>` from the Pi and transmits.
- **Router** — classifies each packet and gives it the right treatment:
  `GPS:`/`TRIPSTART:`/`TRIPEND:`/`SYNC:`/`R*:` → forward to Pi; `QTRIPS:`/`QTRIP:`/
  `QPTS:`/`ACK:` (from Pi) → encrypt + relay over LoRa; `CHAT:` → OLED + history +
  forward (+ auto-`PONG`); `DEVICE:` → heartbeat note + forward.
- **Hub presence** — announces itself as `HubServer` every 60 s (over LoRa for
  peers, and up to the Pi so it appears in the device roster).
- **Local UI** — OLED clock/notifications, RX blink LEDs, buzzer alerts, a
  scrollable history of the **last 20 chat messages** (persisted across reboot),
  GPS-fix indicator, button navigation, RTC.

## Source layout ([../Hub_Server_Firmware/](../Hub_Server_Firmware/))

| File | Role |
|---|---|
| `main.py` | boot, wires modules, runs the loop |
| `lora.py` | raw SX1278 driver (SPI register pokes) |
| `lora_bridge.py` | LoRa ⇄ serial routing, AES, dedup, heartbeat |
| `serial_io.py` | USB serial parse/dispatch |
| `messages.py` | last-N chat ring (flash-persisted) |
| `storage.py` | persistent event log on flash |
| `ui.py` / `display.py` / `leds.py` / `buttons.py` | OLED + LEDs + buttons |
| `clock_rtc.py` / `time_sync.py` | DS1302 RTC |
| `config.py` | pin map, AES key, LoRa air params |

## USB-serial grammar

See [protocols.md → Pi ⇄ Pico A](protocols.md#pi--pico-a-usb-serial). In short:
`TX: / PING / RESET / TIME:` down, `READY / RX: / LOG: / ERR:` up.

## Deploy (MicroPython)

```bash
# from the Pi the Pico A is attached to (e.g. /dev/ttyACM0):
mpremote connect /dev/ttyACM0 fs cp *.py :
mpremote connect /dev/ttyACM0 reset
```
The bridge driver (`hub.py` on the Pi) holds the serial port — **stop it before
running `mpremote`**, then restart it after.

## Gotchas

- SX1278 SPI ops need the `time.sleep_us(50)` delays in `lora.py` — removing them
  breaks reads silently.
- MicroPython `Timer` objects must be held on a module-level reference or GC frees
  them mid-countdown.
- GPS broadcasts are intentionally kept **out** of the chat history buffer (they’d
  flood it); only `CHAT:` reaches the OLED scroll.
