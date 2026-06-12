"""
Hardware Test Script - New Pinout
Tests: LoRa SX1278, OLED (I2C), DS1302 RTC, Buzzer, HC-SR04, 2 Buttons, 6 LEDs
Skips: AHT20/BMP280

Run on the Pico. Watch the serial console for results.
"""

from machine import Pin, SPI, I2C, PWM, Timer
import framebuf
import time

# ============================================================
# PIN MAP (matches your new wiring table)
# ============================================================
# OLED display height - 32 for 0.91", 64 for 0.96"
OLED_HEIGHT = 32

# LoRa SX1278 on SPI0
LORA_SCK  = 18
LORA_MOSI = 19
LORA_MISO = 16
LORA_CS   = 17
LORA_RST  = 20
LORA_DIO0 = 21

# I2C0 (OLED only for this test - AHT20/BMP280 skipped)
I2C_SDA = 0
I2C_SCL = 1

# DS1302 RTC (bit-banged)
RTC_CLK = 2
RTC_DAT = 3
RTC_RST = 4

# Buzzer (PWM)
BUZZER = 5

# HC-SR04
HCSR04_TRIG = 14
HCSR04_ECHO = 15  # via 1k/2k divider!

# Buttons
BTN1 = 12
BTN2 = 13

# LEDs - mirrored layout (right-to-left): green, blue, red | red, blue, green
LEDS = [6, 7, 8, 9, 10, 11]
GREEN_LEDS = [6, 11]   # green right + green left
BLUE_LEDS  = [7, 10]   # blue right + blue left
RED_LEDS   = [8, 9]    # red right + red left

# ============================================================
# Helpers
# ============================================================
def banner(text):
    print("\n" + "=" * 50)
    print(text)
    print("=" * 50)

def ok(msg):    print("  [PASS]", msg)
def fail(msg):  print("  [FAIL]", msg)
def info(msg):  print("        ", msg)

# ---- Status LED helpers ----
# Reuse Pin objects so we don't keep reinitializing
_status_pins = {}
def _led(gpio):
    if gpio not in _status_pins:
        _status_pins[gpio] = Pin(gpio, Pin.OUT)
    return _status_pins[gpio]

def status_off():
    for g in RED_LEDS + GREEN_LEDS + BLUE_LEDS:
        _led(g).value(0)

# Background timer for flashing blue
_blue_timer = None
_blue_state = False

def _blue_flip(_t):
    global _blue_state
    _blue_state = not _blue_state
    for g in BLUE_LEDS:
        _led(g).value(1 if _blue_state else 0)

def status_running():
    """Start blue LEDs flashing in the background; reds/greens off."""
    global _blue_timer, _blue_state
    for g in RED_LEDS + GREEN_LEDS: _led(g).value(0)
    # stop any previous timer cleanly
    if _blue_timer is not None:
        try: _blue_timer.deinit()
        except: pass
    _blue_state = True
    for g in BLUE_LEDS: _led(g).value(1)
    # 4 Hz flash (250ms on, 250ms off)
    _blue_timer = Timer()
    _blue_timer.init(period=250, mode=Timer.PERIODIC, callback=_blue_flip)

def _stop_blue_flash():
    global _blue_timer
    if _blue_timer is not None:
        try: _blue_timer.deinit()
        except: pass
        _blue_timer = None
    for g in BLUE_LEDS: _led(g).value(0)

def status_pass():
    """Stop flashing, blue + green steady."""
    _stop_blue_flash()
    for g in RED_LEDS:               _led(g).value(0)
    for g in BLUE_LEDS + GREEN_LEDS: _led(g).value(1)

def status_fail():
    """Stop flashing, blue + red steady."""
    _stop_blue_flash()
    for g in GREEN_LEDS:           _led(g).value(0)
    for g in BLUE_LEDS + RED_LEDS: _led(g).value(1)

def status_set(passed: bool):
    status_pass() if passed else status_fail()

# ---- OLED status display (shared across tests) ----
# OLED is initialized by the OLED test; if that passes we use it for live status.
_oled = None

def oled_show_status(test_name, state):
    """
    Show test status on the OLED.
    state: 'RUN', 'OK', or 'FAIL'
    """
    if _oled is None:
        return
    try:
        _oled.fill(0)
        # Top: test name (small font, can be up to 16 chars on 128px wide)
        _oled.text(test_name[:16], 0, 0)
        # Big state label below
        if state == 'RUN':
            big_text(_oled, "RUNNING", 8, 12, scale=2)
        elif state == 'OK':
            big_text(_oled, "OK", 40, 12, scale=2)
        elif state == 'FAIL':
            big_text(_oled, "FAIL", 24, 12, scale=2)
        _oled.show()
    except Exception:
        pass  # never let display issues break the test flow

# ---- OLED big-text + marquee helpers ----
def big_text(display, text, x, y, scale=2, color=1):
    """Render built-in 8x8 font scaled up by `scale`."""
    w = 8 * len(text)
    h = 8
    buf = bytearray(w * h // 8 + 1)
    fb = framebuf.FrameBuffer(buf, w, h, framebuf.MONO_HLSB)
    fb.fill(0)
    fb.text(text, 0, 0, 1)
    for py in range(h):
        for px in range(w):
            if fb.pixel(px, py):
                display.fill_rect(x + px * scale,
                                  y + py * scale,
                                  scale, scale, color)

def marquee(display, text, width, y=20, scale=3, speed_ms=30, step=2):
    """Scroll `text` once from right edge to fully off the left."""
    char_w = 8 * scale
    text_w = char_w * len(text)
    for x in range(width, -text_w, -step):
        display.fill(0)
        big_text(display, text, x, y, scale=scale)
        display.show()
        time.sleep_ms(speed_ms)

def draw_smiley(display, cx, cy, r=14, happy=True, depth_ratio=0.5):
    """
    Draw a Cheshire-style grin or frown.
    - Thick crescent that tapers to points at the corners
    - Small upward (smile) or downward (frown) curl flicks at each tip
    cx, cy = center of the mouth's bounding box
    r      = half-width of the main mouth body
    depth_ratio = how deep the curve dips (0.5 = depth equal to half of r)
    """
    mouth_w = r
    max_depth = int(mouth_w * depth_ratio)
    direction = 1 if happy else -1   # 1 = curve dips DOWN (smile), -1 = UP (frown)

    # ---- Main mouth body (thick crescent, tapers at edges) ----
    # The crescent is bounded above by a shallow curve and below by a deep curve.
    # The thickness is max in the middle, ~0 at the tips.
    for x in range(-mouth_w, mouth_w + 1):
        # Normalized horizontal position 0..1 from center
        nx = abs(x) / mouth_w
        # Top edge of crescent (the "lip line"): shallow parabola
        top_offset = int(max_depth * 0.35 * (1 - nx * nx))
        # Bottom edge of crescent: deeper parabola
        bottom_offset = int(max_depth * (1 - nx * nx))
        # Fill vertical line from top to bottom of crescent
        y_top    = cy + direction * top_offset
        y_bottom = cy + direction * bottom_offset
        if direction > 0:
            ya, yb = y_top, y_bottom
        else:
            ya, yb = y_bottom, y_top
        for y in range(min(ya, yb), max(ya, yb) + 1):
            display.pixel(cx + x, y, 1)

    # ---- Corner curl flicks ----
    # Small hook at each tip that curls UP for smile, DOWN for frown.
    # Located just past the main body's tip.
    flick_h = max(3, max_depth // 4)   # height of the flick
    flick_w = max(2, mouth_w // 12)    # how far in from the tip
    # The tip y-position (where main body ends)
    tip_y = cy  # tips of the crescent are at y=cy by our formula
    for side in (-1, 1):
        tip_x = cx + side * mouth_w
        # Draw a short curve going UP from the tip (for smile) or DOWN (for frown).
        # Curl goes "back" toward center horizontally as it goes up/down.
        for i in range(flick_h):
            # As i grows, x moves slightly inward
            dx = side * (flick_w - int(flick_w * (i / flick_h)))
            # And y moves opposite to direction (a smile's tips curl UP, against the dip)
            dy = -direction * (i + 1)
            # Draw 2 pixels for thickness
            display.pixel(tip_x - side * 0 + dx, tip_y + dy, 1)
            display.pixel(tip_x - side * 1 + dx, tip_y + dy, 1)

# ---- DS1302 low-level helpers (bit-banged) ----
_rtc_clk = None
_rtc_dat = None
_rtc_rst = None

def _rtc_init():
    global _rtc_clk, _rtc_dat, _rtc_rst
    _rtc_clk = Pin(RTC_CLK, Pin.OUT, value=0)
    _rtc_dat = Pin(RTC_DAT, Pin.OUT, value=0)
    _rtc_rst = Pin(RTC_RST, Pin.OUT, value=0)

def _rtc_write_byte(b):
    _rtc_dat.init(Pin.OUT)
    for i in range(8):
        _rtc_dat.value((b >> i) & 1)
        _rtc_clk.value(1); time.sleep_us(2)
        _rtc_clk.value(0); time.sleep_us(2)

def _rtc_read_byte():
    _rtc_dat.init(Pin.IN, Pin.PULL_UP)
    v = 0
    for i in range(8):
        v |= ((_rtc_dat.value() & 1) << i)
        _rtc_clk.value(1); time.sleep_us(2)
        _rtc_clk.value(0); time.sleep_us(2)
    return v

def _bcd_to_int(b):  return ((b >> 4) & 0x0F) * 10 + (b & 0x0F)
def _int_to_bcd(n):  return ((n // 10) << 4) | (n % 10)

def rtc_read_all():
    """Returns (year, month, day, hour, minute, second, weekday)."""
    if _rtc_clk is None: _rtc_init()
    # burst read: cmd 0xBF returns 8 bytes (sec, min, hr, date, month, day, year, ctrl)
    _rtc_rst.value(1); time.sleep_us(5)
    _rtc_write_byte(0xBF)
    sec  = _rtc_read_byte()
    minute = _rtc_read_byte()
    hour = _rtc_read_byte()
    date = _rtc_read_byte()
    month = _rtc_read_byte()
    day  = _rtc_read_byte()
    year = _rtc_read_byte()
    _rtc_read_byte()  # control byte
    _rtc_rst.value(0)
    s = _bcd_to_int(sec & 0x7F)   # mask CH bit
    m = _bcd_to_int(minute & 0x7F)
    h = _bcd_to_int(hour & 0x3F)  # 24h mode: lower 6 bits
    d = _bcd_to_int(date & 0x3F)
    mo = _bcd_to_int(month & 0x1F)
    wd = day & 0x07
    y = 2000 + _bcd_to_int(year)
    return (y, mo, d, h, m, s, wd)

def rtc_write_all(year, month, day, hour, minute, second, weekday=1):
    """Set DS1302 in 24-hour mode and start the clock."""
    if _rtc_clk is None: _rtc_init()
    # Disable write-protect: write 0x8E, 0x00
    _rtc_rst.value(1); time.sleep_us(5)
    _rtc_write_byte(0x8E)
    _rtc_write_byte(0x00)
    _rtc_rst.value(0); time.sleep_us(5)
    # Burst write: cmd 0xBE then 8 bytes (CH=0 in seconds byte starts the clock)
    _rtc_rst.value(1); time.sleep_us(5)
    _rtc_write_byte(0xBE)
    _rtc_write_byte(_int_to_bcd(second) & 0x7F)  # CH=0
    _rtc_write_byte(_int_to_bcd(minute))
    _rtc_write_byte(_int_to_bcd(hour) & 0x3F)    # bit 7 = 0 -> 24h mode
    _rtc_write_byte(_int_to_bcd(day))
    _rtc_write_byte(_int_to_bcd(month))
    _rtc_write_byte(_int_to_bcd(weekday) & 0x07)
    _rtc_write_byte(_int_to_bcd(year - 2000))
    _rtc_write_byte(0x00)  # control byte (re-enable WP next time if you want)
    _rtc_rst.value(0)

# ============================================================
# 1. LEDs - blink each one in turn
# ============================================================
def test_leds():
    banner("TEST 1: LEDs (6x)")
    led_pins = [Pin(p, Pin.OUT) for p in LEDS]
    print("  Each LED should blink once in sequence...")
    for i, led in enumerate(led_pins):
        led.value(1)
        print(f"  LED {i+1} (GPIO {LEDS[i]}) ON")
        time.sleep(0.4)
        led.value(0)
    # all on then all off
    for led in led_pins: led.value(1)
    time.sleep(0.5)
    for led in led_pins: led.value(0)
    ok("LED sequence complete - did all 6 light up?")
    return True  # visual test, assume pass

# ============================================================
# 2. Buttons - poll for 5 seconds, ask user to press each
# ============================================================
def test_buttons():
    banner("TEST 2: Buttons (2x)")
    b1 = Pin(BTN1, Pin.IN, Pin.PULL_UP)
    b2 = Pin(BTN2, Pin.IN, Pin.PULL_UP)
    print("  Press BUTTON 1 (GPIO 12) within 5 seconds...")
    seen1 = False
    t0 = time.ticks_ms()
    while time.ticks_diff(time.ticks_ms(), t0) < 5000:
        if b1.value() == 0:
            seen1 = True
            break
        time.sleep_ms(10)
    ok("Button 1 detected") if seen1 else fail("Button 1 NOT detected")

    print("  Press BUTTON 2 (GPIO 13) within 5 seconds...")
    seen2 = False
    t0 = time.ticks_ms()
    while time.ticks_diff(time.ticks_ms(), t0) < 5000:
        if b2.value() == 0:
            seen2 = True
            break
        time.sleep_ms(10)
    ok("Button 2 detected") if seen2 else fail("Button 2 NOT detected")
    return seen1 and seen2

# ============================================================
# 3. Buzzer - short beep with PWM
# ============================================================
def test_buzzer():
    banner("TEST 3: Buzzer")
    try:
        buz = PWM(Pin(BUZZER))
        buz.freq(2000)
        buz.duty_u16(32768)  # 50%
        print("  Beep 1 (2 kHz)...")
        time.sleep(0.3)
        buz.freq(1000)
        print("  Beep 2 (1 kHz)...")
        time.sleep(0.3)
        buz.duty_u16(0)
        buz.deinit()
        ok("Buzzer driven - did you hear two tones?")
        return True
    except Exception as e:
        fail(f"Buzzer error: {e}")
        return False

# ============================================================
# 4. HC-SR04 - take 3 distance readings
# ============================================================
def test_hcsr04():
    banner("TEST 4: HC-SR04 Ultrasonic")
    trig = Pin(HCSR04_TRIG, Pin.OUT)
    echo = Pin(HCSR04_ECHO, Pin.IN)
    trig.value(0)
    time.sleep_ms(50)

    def measure():
        trig.value(0); time.sleep_us(2)
        trig.value(1); time.sleep_us(10)
        trig.value(0)
        # wait for echo high
        t0 = time.ticks_us()
        while echo.value() == 0:
            if time.ticks_diff(time.ticks_us(), t0) > 30000:
                return None
        start = time.ticks_us()
        while echo.value() == 1:
            if time.ticks_diff(time.ticks_us(), start) > 30000:
                return None
        end = time.ticks_us()
        dur = time.ticks_diff(end, start)
        return (dur * 0.0343) / 2  # cm

    print("  Taking 3 readings (point at a wall ~10-100cm away)...")
    got_any = False
    for i in range(3):
        d = measure()
        if d is None:
            fail(f"Reading {i+1}: timeout (out of range or wiring issue)")
        else:
            ok(f"Reading {i+1}: {d:.1f} cm")
            got_any = True
        time.sleep(0.3)
    if not got_any:
        fail("No valid readings - check Trig/Echo wiring AND voltage divider")
    return got_any

# ============================================================
# 5. I2C scan - looking for OLED (typically 0x3C or 0x3D)
# ============================================================
def test_i2c_oled():
    banner("TEST: I2C bus + OLED")
    global _oled
    try:
        i2c = I2C(0, sda=Pin(I2C_SDA), scl=Pin(I2C_SCL), freq=400000)
        devs = i2c.scan()
        if not devs:
            fail("No I2C devices found - check SDA/SCL/power/pull-ups")
            return False
        info(f"Devices found: {[hex(d) for d in devs]}")
        oled_addr = None
        for a in (0x3C, 0x3D):
            if a in devs:
                oled_addr = a
                break
        if oled_addr is None:
            fail("OLED not found at 0x3C or 0x3D")
            return False
        ok(f"OLED found at {hex(oled_addr)}")

        # Try drawing on it (assumes ssd1306 driver is on the Pico)
        try:
            from ssd1306 import SSD1306_I2C
            oled = SSD1306_I2C(128, OLED_HEIGHT, i2c, addr=oled_addr)
            oled.fill(0)
            big_text(oled, "OLED OK", 16, 8, scale=2)
            oled.show()
            ok("OLED drew text - check the screen")
            time.sleep(1.0)
            # Save handle so other tests can use it
            _oled = oled
            return True
        except ImportError:
            info("ssd1306.py not on Pico - skipping draw test")
            info("Upload micropython-ssd1306 to enable display test")
            return True  # I2C device was found, that's enough
        except Exception as e:
            fail(f"OLED draw failed: {e}")
            return False
    except Exception as e:
        fail(f"I2C init failed: {e}")
        return False

# ============================================================
# 6. DS1302 - bit-banged read of seconds register
# ============================================================
def test_ds1302():
    banner("TEST 6: DS1302 RTC")
    clk = Pin(RTC_CLK, Pin.OUT)
    dat = Pin(RTC_DAT, Pin.OUT)
    rst = Pin(RTC_RST, Pin.OUT)
    rst.value(0); clk.value(0)
    time.sleep_us(5)

    def write_byte(b):
        dat.init(Pin.OUT)
        for i in range(8):
            dat.value((b >> i) & 1)
            clk.value(1); time.sleep_us(2)
            clk.value(0); time.sleep_us(2)

    def read_byte():
        dat.init(Pin.IN, Pin.PULL_UP)
        v = 0
        for i in range(8):
            bit = dat.value() & 1
            v |= (bit << i)
            clk.value(1); time.sleep_us(2)
            clk.value(0); time.sleep_us(2)
        return v

    passed = False
    try:
        # First disable write protect: write 0x8E, 0x00
        rst.value(1); time.sleep_us(5)
        write_byte(0x8E)
        write_byte(0x00)
        rst.value(0); time.sleep_us(5)

        # Read seconds register: cmd 0x81
        rst.value(1); time.sleep_us(5)
        write_byte(0x81)
        secs = read_byte()
        rst.value(0)
        # BCD decode lower 7 bits (bit 7 = clock halt)
        ch = (secs >> 7) & 1
        sec_val = ((secs >> 4) & 0x07) * 10 + (secs & 0x0F)
        info(f"Raw seconds byte: {hex(secs)}  (CH={ch}, seconds={sec_val})")
        if 0 <= sec_val <= 59:
            ok("DS1302 responded with a plausible seconds value")
            passed = True
            if ch == 1:
                info("Clock-Halt bit is set - run a time-set routine to start it")
        else:
            fail("Seconds value out of range - check wiring")

        # Read again 1.2s later to confirm it's ticking (only if CH=0)
        if ch == 0 and passed:
            time.sleep(1.2)
            rst.value(1); time.sleep_us(5)
            write_byte(0x81)
            secs2 = read_byte()
            rst.value(0)
            sec_val2 = ((secs2 >> 4) & 0x07) * 10 + (secs2 & 0x0F)
            if sec_val2 != sec_val:
                ok(f"DS1302 is ticking ({sec_val} -> {sec_val2})")
            else:
                fail("DS1302 not ticking - oscillator may not be running")
                passed = False
    except Exception as e:
        fail(f"DS1302 test failed: {e}")
        passed = False
    return passed

# ============================================================
# 7. LoRa SX1278 - read version register (should be 0x12)
# ============================================================
def test_lora():
    banner("TEST 7: LoRa SX1278 (Ra-01)")
    try:
        spi = SPI(0,
                  baudrate=5_000_000,
                  polarity=0, phase=0, bits=8,
                  firstbit=SPI.MSB,
                  sck=Pin(LORA_SCK),
                  mosi=Pin(LORA_MOSI),
                  miso=Pin(LORA_MISO))
        cs  = Pin(LORA_CS,  Pin.OUT, value=1)
        rst = Pin(LORA_RST, Pin.OUT, value=1)

        # Hardware reset
        rst.value(0); time.sleep_ms(10)
        rst.value(1); time.sleep_ms(50)

        # Read version register 0x42 - use the write_readinto pattern
        # that worked in your previous project
        cs.value(0)
        buf = bytearray(2)
        spi.write_readinto(bytes([0x42, 0x00]), buf)
        cs.value(1)
        version = buf[1]
        info(f"REG_VERSION (0x42) = {hex(version)}")
        if version == 0x12:
            ok("LoRa SX1278 detected (version 0x12)")
            return True
        elif version in (0x00, 0xFF):
            fail("No SPI response - check wiring (CS, SCK, MOSI, MISO, RST, 3V3, GND)")
            info("Reminder: ANTENNA must be attached before powering up!")
            return False
        else:
            fail(f"Unexpected version {hex(version)} - wiring or chip variant?")
            return False
    except Exception as e:
        fail(f"LoRa test exception: {e}")
        return False

# ============================================================
# 8. Clock display - shows live 24h time on OLED (runs forever)
# ============================================================
def show_clock(set_time=None):
    """
    Display HH:MM:SS clock on OLED, reading from DS1302.
    Runs forever - press Ctrl-C in Thonny / reset Pico to stop.
    set_time: optional tuple (year, month, day, hour, minute, second)
              to set the RTC before displaying. Pass None to keep current time.
    """
    banner("CLOCK DISPLAY (24h) - runs forever")
    try:
        i2c = I2C(0, sda=Pin(I2C_SDA), scl=Pin(I2C_SCL), freq=400000)
        from ssd1306 import SSD1306_I2C
        oled = SSD1306_I2C(128, OLED_HEIGHT, i2c)
    except Exception as e:
        fail(f"OLED not available: {e}")
        return

    if set_time is not None:
        try:
            rtc_write_all(*set_time)
            ok(f"RTC set to {set_time}")
        except Exception as e:
            fail(f"Could not set RTC: {e}")

    info("Showing clock - press Ctrl-C or reset to stop")
    last_sec = -1
    try:
        while True:
            try:
                y, mo, d, h, mi, s, _ = rtc_read_all()
            except Exception as e:
                fail(f"RTC read failed: {e}")
                return
            if s != last_sec:  # only redraw when seconds change
                last_sec = s
                oled.fill(0)

                # ---- Layout: HH:MM only, big and centered ----
                # Try to fill the screen as much as possible
                hm = "{:02d}:{:02d}".format(h, mi)
                if OLED_HEIGHT >= 64:
                    # Big screen: scale 4 (32px tall), 5 chars * 32 = 160px - too wide
                    # Use scale 3: 24px tall, 120px wide, vertically centered
                    big_text(oled, hm, 4, (OLED_HEIGHT - 24) // 2, scale=3)
                else:
                    # Small 0.91" screen (32px tall)
                    # scale 3 = 24px tall, 120px wide -> fits, centered vertically
                    big_text(oled, hm, 4, (OLED_HEIGHT - 24) // 2, scale=3)

                oled.show()
            time.sleep_ms(100)
    except KeyboardInterrupt:
        oled.fill(0)
        oled.text("Clock stopped", 12, 12)
        oled.show()
        info("Clock stopped by user")


def run_test(test_fn, name, after_pause=0.8):
    """Run a single test, show status on OLED + LEDs, return pass/fail bool."""
    oled_show_status(name, 'RUN')
    status_running()  # blue flashing
    try:
        passed = bool(test_fn())
    except Exception as e:
        fail(f"Unexpected exception: {e}")
        passed = False
    status_set(passed)
    oled_show_status(name, 'OK' if passed else 'FAIL')
    time.sleep(after_pause)
    return passed


def main():
    print("\n" + "#" * 50)
    print("# HARDWARE TEST - new pinout")
    print("# Status LEDs: BLUE flashing = running, GREEN = pass, RED = fail")
    print("# Skipping AHT20/BMP280 as requested")
    print("#" * 50)

    status_off()
    results = {}

    # 1. OLED FIRST - if it works, we use it for live status of remaining tests
    results["OLED"] = run_test(test_i2c_oled, "OLED")

    # 2. LEDs - rainbow visual (skip OLED status during this since LEDs are busy)
    banner("TEST: LEDs (6x)")
    oled_show_status("LEDs", 'RUN')
    results["LEDs"] = test_leds()
    status_set(results["LEDs"])
    oled_show_status("LEDs", 'OK' if results["LEDs"] else 'FAIL')
    time.sleep(0.8)

    # 3-7. Remaining tests - all show status on OLED
    results["Buzzer"]  = run_test(test_buzzer,  "Buzzer")
    results["LoRa"]    = run_test(test_lora,    "LoRa SX1278")
    results["DS1302"]  = run_test(test_ds1302,  "DS1302 RTC")
    results["HC-SR04"] = run_test(test_hcsr04,  "HC-SR04")
    results["Buttons"] = run_test(test_buttons, "Buttons")

    banner("ALL TESTS DONE")
    for name, passed in results.items():
        line = f"  {name:10s} : "
        line += "PASS" if passed else "FAIL"
        print(line)

    # Final summary on LEDs: all-green if everything passed, all-red if any fail
    all_passed = all(results.values())
    status_set(all_passed)

    # Show summary on OLED briefly before clock takes over
    if _oled is not None:
        try:
            _oled.fill(0)
            # Big Cheshire-grin filling the whole display
            cx = 64
            r = 56                    # mouth half-width: spans 8..120 (112px)
            target_depth = OLED_HEIGHT - 8
            depth_ratio = target_depth / r
            if all_passed:
                # Smile: place corners near top (with room for upward flicks),
                # curve dips down to near the bottom.
                draw_smiley(_oled, cx=cx, cy=8, r=r,
                            happy=True, depth_ratio=depth_ratio)
            else:
                # Frown: place corners near bottom, curve rises near top.
                draw_smiley(_oled, cx=cx, cy=OLED_HEIGHT - 8, r=r,
                            happy=False, depth_ratio=depth_ratio)
            _oled.show()
            time.sleep(2)
        except Exception:
            pass

    # Schedule LED shutoff 10 seconds after tests complete (8s remaining
    # since we already showed the summary for 2s above).
    # Using a Timer so it fires while the clock loop runs in foreground.
    def _leds_off(_t):
        status_off()
        print("Status LEDs off (10s post-test).")
    _shutoff = Timer()
    _shutoff.init(period=8000, mode=Timer.ONE_SHOT, callback=_leds_off)

    # ----- Clock display after all tests -----
    # To set the time, uncomment & edit the line below ONCE, run once,
    # then re-comment it (otherwise it resets the clock every boot).
    # Format: (year, month, day, hour, minute, second)
    SET_TIME = None
    # SET_TIME = (2026, 4, 30, 14, 30, 0)   # <- example: 30 Apr 2026, 14:30:00

    show_clock(set_time=SET_TIME)

if __name__ == "__main__":
    main()