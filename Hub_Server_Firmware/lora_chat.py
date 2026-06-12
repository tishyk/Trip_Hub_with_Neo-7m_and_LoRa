"""
lora_chat.py - Interactive LoRa sender for the OLD-pinout Pico.

Run this in Thonny on Pico B (old pins from the reference card):
    SCK=GPIO2, MOSI=GPIO3, MISO=GPIO4, NSS=GPIO5, RST=GPIO22, DIO0=GPIO26

Type messages and press Enter to send them over LoRa.
The new tracker Pico (Pico A) is configured to receive on the same
frequency / SF / BW / sync-word, so it will pick them up and the Pi 5
running hub.py will log them.

Usage:
    Click Run in Thonny. You'll see:

      LoRa Sender Ready
      Type message + Enter to send. Type 'q' to quit.
      > hello world
      [TX 1] hello world
      > test
      [TX 2] test
      > q
      Bye.

Settings must match the bridge exactly:
    433 MHz, SF7, BW125, CR4/5, sync 0x34, CRC on

Encryption:
    Set LORA_KEY to the SAME 16-byte key as on the receiver (config.LORA_KEY
    on Pico A). Set to None to send plaintext.
"""

import time
import os
import sys
import select
from machine import SPI, Pin
try:
    import cryptolib
except ImportError:
    cryptolib = None


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
# Encryption key - MUST match config.LORA_KEY on the receiver
# ============================================================
LORA_KEY = b"LoRaMeshDemoKey1"   # 16 bytes, or None to disable encryption


# ============================================================
# Registers / modes (same as new tracker, copied for standalone use)
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
# Encryption helper
# ============================================================
# MicroPython's cryptolib supports different modes depending on the
# firmware build. We try CBC first (best), fall back to ECB if needed.
_AES_ECB = 1
_AES_CBC = 2
_BLOCK = 16

# Some MicroPython builds need bytearray for the key.
_KEY_BA = bytearray(LORA_KEY) if LORA_KEY else None

# Detect which AES mode actually works on this firmware
_AES_MODE = None
if cryptolib is not None and _KEY_BA is not None:
    # Try CBC
    try:
        _ = cryptolib.aes(_KEY_BA, _AES_CBC, bytearray(b"\x00" * 16))
        _AES_MODE = _AES_CBC
        print("Crypto: AES-CBC mode available")
    except Exception:
        # Try ECB
        try:
            _ = cryptolib.aes(_KEY_BA, _AES_ECB)
            _AES_MODE = _AES_ECB
            print("Crypto: AES-ECB mode (no CBC support in firmware)")
        except Exception as e:
            print("Crypto: AES not supported -", e)


def _pkcs7_pad(data):
    pad_len = _BLOCK - (len(data) % _BLOCK)
    return data + bytes([pad_len]) * pad_len


def _pkcs7_unpad(data):
    if not data or len(data) % _BLOCK != 0:
        return None
    pad_len = data[-1]
    if pad_len < 1 or pad_len > _BLOCK:
        return None
    for i in range(1, pad_len + 1):
        if data[-i] != pad_len:
            return None
    return data[:-pad_len]


def encrypt(plaintext):
    """Encrypt plaintext. Returns IV+ciphertext (CBC) or just ciphertext (ECB).
    Returns plaintext unchanged if encryption is disabled."""
    if isinstance(plaintext, str):
        plaintext = plaintext.encode("utf-8")
    if _AES_MODE is None or _KEY_BA is None:
        return plaintext

    padded = _pkcs7_pad(plaintext)
    if _AES_MODE == _AES_CBC:
        iv = os.urandom(_BLOCK)
        cipher = cryptolib.aes(_KEY_BA, _AES_CBC, bytearray(iv))
        return iv + cipher.encrypt(padded)
    else:  # ECB
        cipher = cryptolib.aes(_KEY_BA, _AES_ECB)
        return cipher.encrypt(padded)


def decrypt(blob):
    """Decrypt blob. Returns plaintext bytes, or None on failure.
    Returns input unchanged if encryption is disabled."""
    if _AES_MODE is None or _KEY_BA is None:
        return blob if isinstance(blob, (bytes, bytearray)) else None
    if not isinstance(blob, (bytes, bytearray)):
        return None
    try:
        if _AES_MODE == _AES_CBC:
            if len(blob) < _BLOCK * 2 or len(blob) % _BLOCK != 0:
                return None
            iv = bytes(blob[:_BLOCK])
            ciphertext = bytes(blob[_BLOCK:])
            cipher = cryptolib.aes(_KEY_BA, _AES_CBC, bytearray(iv))
        else:  # ECB
            if len(blob) < _BLOCK or len(blob) % _BLOCK != 0:
                return None
            ciphertext = bytes(blob)
            cipher = cryptolib.aes(_KEY_BA, _AES_ECB)
        padded = cipher.decrypt(ciphertext)
    except Exception:
        return None
    return _pkcs7_unpad(padded)


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

        # +20 dBm, max LNA, AGC auto
        self.write_reg(REG_PA_CONFIG,      0xFF)
        self.write_reg(REG_LNA,            0x23)
        self.write_reg(REG_MODEM_CONFIG_3, 0x04)

        # BW125, CR4/5, SF7, CRC on  (must match the bridge)
        self.write_reg(REG_MODEM_CONFIG_1, 0x72)
        self.write_reg(REG_MODEM_CONFIG_2, 0x74)

        # Preamble 8, sync 0x34
        self.write_reg(REG_PREAMBLE_MSB, 0x00)
        self.write_reg(REG_PREAMBLE_LSB, 0x08)
        self.write_reg(REG_SYNC_WORD,    0x34)

        # FIFO base
        self.write_reg(REG_FIFO_TX_BASE, 0x00)
        self.write_reg(REG_FIFO_RX_BASE, 0x00)

        print("Radio initialized (version=0x%02x, 433 MHz, SF7, BW125, sync 0x34)" % v)

        # Start in receive mode so we hear incoming messages
        self.rx_mode()

    def rx_mode(self):
        """Put radio into RX_CONTINUOUS mode."""
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
        # Encrypt (no-op if key disabled)
        payload = encrypt(payload)
        if len(payload) > 250:
            payload = payload[:250]

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
        # Return to RX mode so we hear replies
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

    print()
    print("LoRa Chat Ready (bidirectional)")
    if _KEY_BA and _AES_MODE is not None:
        mode_name = "CBC" if _AES_MODE == _AES_CBC else "ECB"
        print("Encryption: AES-128-%s ON" % mode_name)
    elif LORA_KEY and not cryptolib:
        print("WARNING: LORA_KEY set but cryptolib not available - sending plaintext!")
    else:
        print("Encryption: OFF (plaintext)")
    print("Type message + Enter to send. Type 'q' to quit.")
    print("Incoming messages will appear automatically.")
    print()

    counter = 0
    rx_counter = 0
    poller = select.poll()
    poller.register(sys.stdin, select.POLLIN)
    line_buf = ""
    sys.stdout.write("> ")

    while True:
        # ---- Check for incoming LoRa packets ----
        got = radio.poll_rx()
        if got is not None:
            data, rssi, snr = got
            decrypted = decrypt(data)
            if decrypted is None:
                print("\n[RX BAD] decrypt failed (wrong key or plain packet?)  rssi=%d" % rssi)
            else:
                try:
                    text = decrypted.decode("utf-8")
                except UnicodeError:
                    text = decrypted.hex()
                rx_counter += 1
                # Erase the prompt, print the message, redraw the prompt + buffer
                print("\n[RX %d] %s  (rssi=%d snr=%.1f)" % (rx_counter, text, rssi, snr))
            sys.stdout.write("> " + line_buf)

        # ---- Check for typed characters (non-blocking) ----
        if poller.poll(50):  # 50ms
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


# Run when imported / executed
chat()