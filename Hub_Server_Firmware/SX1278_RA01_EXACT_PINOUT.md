# SX1278 / Ra-01 LoRa Module - Exact Pinout Guide

## Your Modules Identified ✅

**Image 1:** SX1278 LoRa Module (Blue board)
- Large spiral antenna coil
- 8 pin header
- 410-525 MHz FSK
- Perfect for Trip Tracker!

**Image 2:** Ra-01 Module (Tiny black square)
- Compact version
- Same SX1278 chip inside
- 8 pins (smaller header)
- Same pinout!

## Exact Pin Configuration

### SX1278 Module Pins (Left to Right)

```
Pin 1  → GND
Pin 2  → MOSI (DIN)
Pin 3  → MISO (DOUT)
Pin 4  → SCK (CLK)
Pin 5  → NSS/CS
Pin 6  → RESET
Pin 7  → DIO0
Pin 8  → 3.3V
```

### Ra-01 Module Pins (Same Layout)

```
Pin 1  → GND
Pin 2  → MOSI
Pin 3  → MISO
Pin 4  → SCK
Pin 5  → NSS/CS
Pin 6  → RESET
Pin 7  → DIO0
Pin 8  → 3.3V
```

## Wiring to Raspberry Pi Pico

```
SX1278/Ra-01    →    Pico RP2040
════════════════════════════════

Pin 1 (GND)     →    GND (Pin 3, 8, 13, 18, 23, 28, 33, 38)
Pin 2 (MOSI)    →    GPIO 3 (SPI0 MOSI) - Pin 7
Pin 3 (MISO)    →    GPIO 4 (SPI0 MISO) - Pin 6
Pin 4 (SCK)     →    GPIO 2 (SPI0 SCK)  - Pin 4
Pin 5 (NSS/CS)  →    GPIO 5 (SPI0 CS)   - Pin 9
Pin 6 (RESET)   →    GPIO 22            - Pin 29
Pin 7 (DIO0)    →    GPIO 26 (INT/IRQ)  - Pin 31
Pin 8 (3.3V)    →    3V3 (Pin 36)
```

## Pico Pinout Reference

```
Pico RP2040 Top View
════════════════════

GPIO0   GPIO1   GND     GPIO2   GPIO3   GPIO4
 1      2       3       4       5       6     
[SCK0] [MOSI0] [GND]  [SCK0]  [MOSI0] [MISO0]
                                            ↓ Pin 6
                                       [GPIO 4 - MISO]

GPIO5   GPIO6   GPIO7   GPIO8   GPIO9   GPIO10
 7      8       9      10      11       12
[NSS0]  ...     ...     ...     ...      ...
   ↓ Pin 9
 [GPIO 5 - CS]

...

GPIO22  GPIO23  GPIO24  GPIO25  GPIO26  GPIO27
29      30      31      32      33      34
[RST]  [RX]    [TX]    [DEBUG] [INT]   ...
  ↓ Pin 29      ↓ Pin 31
[GPIO 22]   [GPIO 26 - DIO0]

...

3V3     GND
36      38
[3.3V]  [GND]
```

## Complete Wiring Table

| SX1278 Pin | Function | Pico Pin | GPIO | Purpose |
|------------|----------|----------|------|---------|
| 1 | GND | 3, 8, 13, 18, 23, 28, 33, 38 | - | Ground |
| 2 | MOSI | 7 | GPIO 3 | SPI Data In |
| 3 | MISO | 6 | GPIO 4 | SPI Data Out |
| 4 | SCK | 4 | GPIO 2 | SPI Clock |
| 5 | NSS/CS | 9 | GPIO 5 | SPI Chip Select |
| 6 | RESET | 29 | GPIO 22 | Reset Signal |
| 7 | DIO0 | 31 | GPIO 26 | Interrupt/RX Done |
| 8 | 3.3V | 36 | - | Power Supply |

## Wiring Diagram (Text)

```
┌─────────────────────┐
│ SX1278 Module       │
│  (Blue Board)       │
├─────────────────────┤
│ 1 GND    ─────────→ GND (Pico Pin 3)
│ 2 MOSI   ─────────→ GPIO 3 (Pico Pin 7)
│ 3 MISO   ─────────→ GPIO 4 (Pico Pin 6)
│ 4 SCK    ─────────→ GPIO 2 (Pico Pin 4)
│ 5 NSS/CS ─────────→ GPIO 5 (Pico Pin 9)
│ 6 RESET  ─────────→ GPIO 22 (Pico Pin 29)
│ 7 DIO0   ─────────→ GPIO 26 (Pico Pin 31)
│ 8 3.3V   ─────────→ 3V3 (Pico Pin 36)
└─────────────────────┘
     ↓
   Antenna Coil
```

## Python Code for This Module

```python
from machine import SPI, Pin
from sx127x_fsk import SX127xFSK
import time

# SPI Configuration
spi = SPI(0, 
    baudrate=10000000,
    polarity=0,
    phase=0,
    bits=8,
    firstbit=SPI.MSB,
    sck=Pin(2),
    mosi=Pin(3),
    miso=Pin(4)
)

# Pin Definitions (Exact)
pins = {
    'ss': 5,        # GPIO 5 (NSS/CS)
    'rst': 22,      # GPIO 22 (RESET)
    'dio_0': 26,    # GPIO 26 (DIO0)
}

# Module Parameters
parameters = {
    'frequency': 433E6,           # 433 MHz
    'tx_power_level': 15,         # 15 dBm
    'bit_rate': 9600,             # 9600 bps
    'frequency_deviation': 5000,  # 5 kHz
    'bandwidth': 50000,           # 50 kHz
    'preamble_length': 16,        # 16 bytes
    'sync_word': 0x2d,            # 0x2D
    'enable_CRC': True,
}

# Initialize Module
print("Initializing SX1278 module...")
lora = SX127xFSK(spi, pins, parameters)
print("✅ Module ready!")

# Send Test
print("Sending test message...")
lora.println("Hello from Pico!")
print("✅ Sent!")

# Receive Test
print("Listening for 10 seconds...")
lora.receive()
start_time = time.time()

while time.time() - start_time < 10:
    if lora.received_packet():
        payload = lora.read_payload()
        rssi = lora.packet_rssi()
        print(f"✅ Received: {payload.decode()}")
        print(f"   RSSI: {rssi} dBm")
    time.sleep(0.1)

print("Test complete!")
```

## Physical Assembly Tips

### Soldering
1. **Clean pins** with desoldering wick if needed
2. **Pre-tin** all pins (add small solder blob)
3. **Use flux** for better solder flow
4. **Heat both wire and pad** (2-3 seconds)
5. **Use thin solder** (0.5mm or smaller)
6. **Check for shorts** with multimeter

### Cable Management
```
Use a breakout board if available:
━━━━━━━━━━━━━━━━━━━━━━
   [SX1278 Module]
        ↓
   [Breakout Board]
        ↓
  [To Pico via ribbon]
```

Or use individual wires:
- **Colored wires** (easier to track)
- **Heatshrink tubing** (prevent shorts)
- **Label each wire** (avoid confusion)

### Antenna
- **Keep antenna coil clear** (no obstacles)
- **Orient perpendicular** to other antennas
- **17cm long wire** (optimal for 433 MHz)
- **Keep away from metal** (use plastic case)

## Testing Checklist

```
Before using with Trip Tracker:

Hardware:
☐ All 8 wires connected correctly
☐ No cold solder joints
☐ No shorts (test with multimeter)
☐ Power LED on (if present)
☐ Antenna coil intact

Software:
☐ SPI works (read version register)
☐ Can transmit (serial output shows "Sent")
☐ Can receive (with second module)
☐ RSSI shows reasonable values (-120 to 0 dBm)
☐ No error messages in console

Communication:
☐ Both modules same frequency (433 MHz)
☐ Both modules same bit rate (9600)
☐ Both modules same sync word (0x2d)
☐ Range at least 100 meters
☐ No data corruption in messages
```

## Troubleshooting Quick Reference

| Problem | Solution |
|---------|----------|
| Module not detected | Check all 8 wires connected |
| SPI errors | Verify GPIO 2,3,4,5 are free |
| No transmission | Check DIO0 pin (GPIO 26) |
| No reception | Swap RX/TX or check sync word |
| Short range (<10m) | Raise TX power to 20 dBm |
| Interference | Lower TX power to 10 dBm |
| Checksum errors | Enable CRC on both sides |

## Integration with Trip Tracker

Just use the code exactly as shown above, then in `trip_tracker_enhanced.py`:

```python
from sx127x_fsk import SX127xFSK

# Paste the initialization code above
# Then use:

# Send event
lora.println("Trip started: WALKING")

# Or with JSON
import json
event = {'type': 'TRIP_START', 'movement': 'WALKING'}
lora.println(json.dumps(event))
```

## Summary

✅ **Modules:** SX1278 / Ra-01 (both work!)
✅ **Frequency:** 433 MHz ISM band
✅ **Interface:** SPI (standard)
✅ **Pins:** Exactly as shown above
✅ **Driver:** sx127x_fsk.py
✅ **Range:** 5-10 km line-of-sight
✅ **Speed:** 9600 bps
✅ **Power:** 15 dBm recommended

Your exact module is **100% compatible** with Trip Tracker! 🚀

Just follow the wiring above and you're good to go!
