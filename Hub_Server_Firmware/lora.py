"""
lora.py - SX1278 (Ra-01) driver for the Pico tracker.

Uses the new wiring table pins:
  SCK=18, MOSI=19, MISO=16, NSS=17, RST=20, DIO0=21

Re-uses the proven SPI pattern from the working Pico-Pico bridge:
  - write_readinto for reads (data is in buf[1])
  - simple write for writes
  - same register settings: 433 MHz, SF9, BW125, CR4/5, CRC on, sync 0x34
"""

import time
from machine import SPI, Pin

# Pin assignments (new tracker wiring)
LORA_SCK  = 18
LORA_MOSI = 19
LORA_MISO = 16
LORA_CS   = 17
LORA_RST  = 20
LORA_DIO0 = 21

# Register addresses
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

# Modes
MODE_LORA            = 0x80
MODE_SLEEP           = 0x00
MODE_STDBY           = 0x01
MODE_TX              = 0x03
MODE_RX_CONTINUOUS   = 0x05

# IRQ flags
IRQ_TX_DONE          = 0x08
IRQ_RX_DONE          = 0x40
IRQ_PAYLOAD_CRC_ERR  = 0x20


class LoRa:
    def __init__(self):
        self.spi = SPI(0, baudrate=5_000_000,
                       sck=Pin(LORA_SCK),
                       mosi=Pin(LORA_MOSI),
                       miso=Pin(LORA_MISO))
        self.cs  = Pin(LORA_CS,  Pin.OUT, value=1)
        self.rst = Pin(LORA_RST, Pin.OUT, value=1)
        self.dio0 = Pin(LORA_DIO0, Pin.IN)
        # Optional callbacks the app sets to get notified about radio activity
        self.on_tx_start = None  # called just before transmission begins
        self.on_tx_end   = None  # called after TxDone
        self.on_rx       = None  # called after a packet is received (bytes)
        self.reset()
        self.init_radio()

    def reset(self):
        self.rst.value(0)
        time.sleep(0.1)
        self.rst.value(1)
        time.sleep(0.5)

    def read_reg(self, reg):
        self.cs.value(0)
        buf = bytearray(2)
        self.spi.write_readinto(bytes([reg & 0x7F, 0x00]), buf)
        self.cs.value(1)
        time.sleep_us(50)
        return buf[1]

    def write_reg(self, reg, value):
        self.cs.value(0)
        self.spi.write(bytes([reg | 0x80, value & 0xFF]))
        self.cs.value(1)
        time.sleep_us(50)

    def init_radio(self):
        v = self.read_reg(REG_VERSION)
        if v != 0x12:
            raise RuntimeError("SX1278 not found, version=0x%02x" % v)

        # Sleep -> LoRa -> standby
        self.write_reg(REG_OP_MODE, MODE_LORA | MODE_SLEEP)
        time.sleep(0.01)
        self.write_reg(REG_OP_MODE, MODE_LORA | MODE_STDBY)
        time.sleep(0.01)

        # 433 MHz
        self.write_reg(REG_FRF_MSB, 0x6C)
        self.write_reg(REG_FRF_MID, 0x80)
        self.write_reg(REG_FRF_LSB, 0x00)

        # +20dBm, max LNA
        self.write_reg(REG_PA_CONFIG,      0xFF)
        self.write_reg(REG_LNA,            0x23)

        # BW125, CR4/5, SF+CRC — MUST match runtime.py on Pico B + ESP32-C3.
        # Change LORA_SF to tune range vs airtime:
        #   SF9  -> ~2x range vs SF7,  ~4x airtime
        #   SF10 -> ~3x range vs SF7,  ~8x airtime
        #   SF11 -> ~4x range vs SF7, ~16x airtime  (current — max range mode)
        LORA_SF = 9   # <-- change here + runtime.py on Pico B + ESP32-C3 config.h
        # AGC auto (bit 2). LowDataRateOptimize (bit 3) MUST be set when
        # symbol time > 16 ms, i.e. SF11/SF12 at BW125 — datasheet §4.1.1.6.
        # Forgetting this bit causes random decode failures at high SF.
        ldro = 0x08 if LORA_SF >= 11 else 0x00
        self.write_reg(REG_MODEM_CONFIG_3, 0x04 | ldro)
        self.write_reg(REG_MODEM_CONFIG_1, 0x72)             # BW125 CR4/5
        self.write_reg(REG_MODEM_CONFIG_2, (LORA_SF << 4) | 0x04)   # SF+CRC

        # Preamble 8, sync 0x34 (public)
        self.write_reg(REG_PREAMBLE_MSB, 0x00)
        self.write_reg(REG_PREAMBLE_LSB, 0x08)
        self.write_reg(REG_SYNC_WORD,    0x34)

        # FIFO base
        self.write_reg(REG_FIFO_TX_BASE, 0x00)
        self.write_reg(REG_FIFO_RX_BASE, 0x00)

        self.rx_mode()

    def rx_mode(self):
        self.write_reg(REG_IRQ_FLAGS, 0xFF)
        self.write_reg(REG_FIFO_ADDR_PTR, 0x00)
        self.write_reg(REG_OP_MODE, MODE_LORA | MODE_RX_CONTINUOUS)
        time.sleep(0.005)

    def send(self, payload):
        if isinstance(payload, str):
            payload = payload.encode('utf-8')
        if len(payload) > 250:
            payload = payload[:250]

        if self.on_tx_start: self.on_tx_start()

        self.write_reg(REG_OP_MODE,  MODE_LORA | MODE_STDBY)
        self.write_reg(REG_IRQ_FLAGS, 0xFF)
        self.write_reg(REG_FIFO_ADDR_PTR, 0x00)
        for b in payload:
            self.write_reg(REG_FIFO, b)
        self.write_reg(REG_PAYLOAD_LENGTH, len(payload))
        self.write_reg(REG_OP_MODE, MODE_LORA | MODE_TX)

        # Wait for TxDone (max 3s)
        t0 = time.ticks_ms()
        sent = False
        while time.ticks_diff(time.ticks_ms(), t0) < 3000:
            if self.read_reg(REG_IRQ_FLAGS) & IRQ_TX_DONE:
                sent = True
                break
            time.sleep_ms(2)

        self.write_reg(REG_IRQ_FLAGS, 0xFF)
        if self.on_tx_end: self.on_tx_end()
        self.rx_mode()
        return sent

    def poll_rx(self):
        """Return (bytes, rssi_dbm, snr_db) or None."""
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
        rssi_dbm = rssi_raw - 164  # 433 MHz band offset

        snr_raw = self.read_reg(REG_PKT_SNR_VALUE)
        if snr_raw > 127: snr_raw -= 256
        snr_db = snr_raw / 4.0

        result = (bytes(data), rssi_dbm, snr_db)
        if self.on_rx: self.on_rx(result)
        return result