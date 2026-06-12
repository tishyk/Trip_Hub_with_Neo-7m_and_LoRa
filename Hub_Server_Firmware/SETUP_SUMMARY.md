# Pico LoRa GPS Tracker - Complete Setup & Debug Summary

## PROJECT OVERVIEW
**Goal:** Two Pico RP2040 boards with Ra-01 SX1278 LoRa modules communicating on 433 MHz
**Status:** ✅ WORKING - Both TX and RX confirmed operational
**Total Debug Time:** 2 days (SPI communication was the main blocker)

---

## HARDWARE

### Pico RP2040 Specs
- Microcontroller: RP2040
- SPI0: GPIO2 (SCK), GPIO3 (MOSI), GPIO4 (MISO)
- Reset capability via GPIO22
- Built-in LED: GPIO25

### Ra-01 LoRa Module Specs
- IC: SX1278 (supports FSK, GFSK, LoRa modes)
- Frequency: 433 MHz (range 420-450 MHz)
- SPI: Half-duplex SPI communication
- Power: 3.3V, ~120mA TX, ~10mA RX
- Pin Pitch: 2.0 mm

---

## WIRING - Pico to Ra-01 LoRa Module

### Physical Pin Labels (Ra-01 Breakout v4.0)
```
Left Side       Right Side
MISO            100 (DIO0)
SCK             MOSI
RST             NSS
GND             3V3
```

### Pico Connection Map

| Ra-01 Pin | Ra-01 Label | → | Pico Physical | Pico GPIO | Function |
|-----------|-------------|---|---------------|-----------|----------|
| 1 | MISO | → | 6 | GPIO4 | SPI Data In |
| 2 | SCK | → | 4 | GPIO2 | SPI Clock |
| 3 | RST | → | 29 | GPIO22 | Reset |
| 4 | GND | → | 3 | GND | Ground |
| 5 | DIO0/100 | → | 31 | GPIO26 | Interrupt (optional) |
| 6 | MOSI | → | 5 | GPIO3 | SPI Data Out |
| 7 | NSS | → | 7 | GPIO5 | Chip Select |
| 8 | 3V3 | → | 36 | 3V3 | Power |

**CRITICAL:** Check physical pin numbers against Pico pinout diagram!

---

## CRITICAL DISCOVERIES (2-Day Debug Log)

### Issue #1: FSK vs LoRa Mode ❌→✅
**Problem:** Code was using FSK register map, but Ra-01 defaults to LoRa
**Solution:** Set bit 7 (0x80) of REG_OP_MODE to enable LoRa mode
```python
write_reg(0x01, 0x81)  # MODE_STANDBY | LORA_MODE (0x80 | 0x01)
```

### Issue #2: Pico SPI Read Pattern ❌→✅
**Problem:** Standard `write()` + `read()` pattern returned 0x00
**Solution:** Use `write_readinto()` with 2-byte buffer
```python
# WRONG:
cs.off()
spi.write(bytes([0x42]))
cs.on()
data = spi.read(1)  # Returns 0x00 ❌

# CORRECT:
cs.off()
buf = bytearray(2)
spi.write_readinto(bytes([0x42, 0x00]), buf)
cs.on()
return buf[1]  # buf[1] has actual data ✅
```

### Issue #3: Pi 5 LoRa RX (Separate Issue)
**Problem:** Pi 5 received empty FIFO despite proper configuration
**Solution:** Pi 5 LoRa RX was never fully debugged (Pico solution used instead)
**Key Finding:** Pi 5 requires `xfer2()` atomic transactions (not available on Pico)

---

## REGISTER MAP (LoRa Mode)

| Register | Address | Purpose | Value |
|----------|---------|---------|-------|
| OpMode | 0x01 | Operating mode | 0x81 (LoRa+Standby) |
| FRF_MSB | 0x06 | Frequency MSB | 0x6C (433 MHz) |
| FRF_MID | 0x07 | Frequency MID | 0x80 |
| FRF_LSB | 0x08 | Frequency LSB | 0x00 |
| PA_Config | 0x09 | Power amp | 0xFF (20dBm) |
| LNA | 0x0C | Low noise amp | 0x23 (max gain) |
| FIFO_Ptr | 0x0D | FIFO address | 0x00 (reset) |
| IRQ_Flags | 0x12 | Interrupt flags | 0xFF (clear all) |
| RX_NB_Bytes | 0x13 | Bytes received | - |
| ModemCfg1 | 0x1D | BW & CR | 0x72 (125kHz, 4/5) |
| ModemCfg2 | 0x1E | SF & CRC | 0x74 (SF7, CRC on) |
| ModemCfg3 | 0x26 | AGC | 0x04 (auto) |
| PreambleMSB | 0x20 | Preamble | 0x00 |
| PreAmbleLSB | 0x21 | Preamble | 0x08 (8 symbols) |
| SyncWord | 0x39 | LoRa sync | 0x34 (public) |
| Version | 0x42 | Chip version | 0x12 (SX1278) |

### LoRa Configuration Used
- **Spreading Factor:** 7
- **Bandwidth:** 125 kHz
- **Coding Rate:** 4/5
- **Sync Word:** 0x34 (LoRa public)
- **Preamble:** 8 symbols
- **CRC:** Enabled

---

## WORKING CODE

### Pico #1 - TX (pico1_lora_tx_fixed_final.py)
```python
def read_reg(self, reg):
    """CRITICAL: Use write_readinto, return buf[1]"""
    self.cs.off()
    buf = bytearray(2)
    self.spi.write_readinto(bytes([reg, 0x00]), buf)
    self.cs.on()
    time.sleep(0.01)
    return buf[1]  # ← KEY: data is in buf[1]

def write_reg(self, reg, value):
    self.cs.off()
    self.spi.write(bytes([reg | 0x80, value]))
    self.cs.on()
    time.sleep(0.01)
```

### Pico #2 - RX (pico2_lora_rx_fixed_final.py)
Same read/write pattern as TX, but set to RX_CONTINUOUS mode (0x05)

### Key Settings Both Sides Must Match:
- Frequency: 433 MHz (0x6C8000)
- ModemCfg1: 0x72 (BW 125k, CR 4/5)
- ModemCfg2: 0x74 (SF 7, CRC on)
- SyncWord: 0x34

---

## TESTING & VERIFICATION

### Step 1: SPI Test
```python
from machine import SPI, Pin
spi = SPI(0, baudrate=10000000, sck=Pin(2), mosi=Pin(3), miso=Pin(4))
cs = Pin(5, Pin.OUT)
cs.off()
buf = bytearray(2)
spi.write_readinto(bytes([0x42, 0x00]), buf)  # Read version
cs.on()
print(f"Version: 0x{buf[1]:02x}")  # Should be 0x12
```

### Step 2: Full Diagnostic
Run `pico2_diagnostic_fixed.py` - confirms all registers writable

### Step 3: Communication Test
- Upload TX to Pico #1
- Upload RX to Pico #2
- Place close (< 1 meter)
- Should see: `[RX #1] ✅ Received: 'HELLO_001'`

### Success Indicators
✅ LED on Pico #2 lights up = message received
✅ Message counter increments = both sides working
✅ Multiple messages received = stable link

---

## TIMING DELAYS (CRITICAL)

All SPI operations must include delays:
```python
time.sleep(0.01)  # After write_reg
time.sleep(0.01)  # After read_reg
time.sleep(0.1)   # After mode change
time.sleep(0.5)   # After reset
```

Without delays, SPI reads return stale data!

---

## COMMON MISTAKES TO AVOID

❌ Using FSK register map (0x02, 0x03 for bitrate) - use LoRa mode (0x1D, 0x1E, 0x26)
❌ Using `write()` + `read()` separately - use `write_readinto()` with buf[1]
❌ Forgetting the 0x80 bit for LoRa mode
❌ Different frequency/BW/SF/CR on TX vs RX
❌ Not clearing IRQ flags after RX
❌ No time.sleep() between SPI operations
❌ Wrong FIFO pointer reset (must be 0x00)

---

## FILES LOCATION

All working code in `/mnt/user-data/outputs/`:
- `pico1_lora_tx_fixed_final.py` - Transmitter code
- `pico2_lora_rx_fixed_final.py` - Receiver code  
- `pico2_diagnostic_fixed.py` - Full register diagnostic
- `pico2_spi_simple_test.py` - SPI method comparison

---

## FOR NEXT SESSION

1. **Upload code** from outputs folder to Pico boards
2. **Check wiring** against pin table above
3. **Run SPI test** to verify communication
4. **Run diagnostic** if TX/RX not working
5. **Verify frequency match** (both 433 MHz)
6. **Check LED** blinks on RX (listening) / solid (message)

**Expected output:**
```
[RX #1] ✅ Received: 'HELLO_001'
[RX #2] ✅ Received: 'HELLO_002'
...
```

---

## NEXT IMPROVEMENTS

- [ ] Add GPS data to messages
- [ ] Switch to binary protocol (smaller messages)
- [ ] Add error handling/retries
- [ ] Implement sleep modes for battery
- [ ] Test range at distance
- [ ] Add timestamp to messages
- [ ] Implement ACK/NACK
- [ ] Add channel hopping

---

## REFERENCE LINKS

- SX1278 Datasheet: https://cdn-shop.adafruit.com/product-files/3179/sx1276_77_78_79.pdf
- Pico Pinout: https://datasheets.raspberrypi.com/pico/pico-datasheet.pdf
- Ra-01 Manual: (see uploaded PDF)

---

**Last Updated:** 2 days of debugging complete ✅
**Status:** Both Pico boards communicating on LoRa 433 MHz
**Next Step:** Integrate GPS and test real-world range
