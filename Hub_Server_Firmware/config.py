"""
config.py - all hardware pins and timing constants in one place.
Edit values here, not scattered across other files.
"""

# ---- OLED ----
I2C_SDA     = 0
I2C_SCL     = 1
OLED_HEIGHT = 32      # 32 for 0.91", 64 for 0.96"

# ---- DS1302 RTC ----
RTC_CLK = 2
RTC_DAT = 3
RTC_RST = 4

# ---- Buzzer ----
BUZZER = 5

# ---- LEDs (mirrored layout: g/b/r | r/b/g) ----
RED_RIGHT   = 8        # generic alert: alternating side-to-side red flash
RED_LEFT    = 9
GREEN_LEFT  = 6        # missed-message alert: alternating green flash
GREEN_RIGHT = 11
BLUE_LEFT = 7
BLUE_RIGHT = 10
GREEN_LEDS  = [GREEN_LEFT, GREEN_RIGHT]
BLUE_LEDS   = [BLUE_LEFT, BLUE_RIGHT]
RED_LEDS    = [RED_LEFT, RED_RIGHT]
ALL_LEDS    = [GREEN_LEFT, BLUE_LEFT, RED_RIGHT, RED_LEFT, BLUE_RIGHT, GREEN_RIGHT]

# ---- Buttons ----
BTN_LEFT  = 12
BTN_RIGHT = 13

# ---- HC-SR04 ----
HCSR04_TRIG = 14
HCSR04_ECHO = 15

# ---- LoRa SX1278 ----
LORA_SCK  = 18
LORA_MOSI = 19
LORA_MISO = 16
LORA_CS   = 17
LORA_RST  = 20
LORA_DIO0 = 21

# ---- Timing / behavior ----
MSG_LIFETIME_MS      = 10_000   # legacy — kept for any other caller; ui.py now
                                # holds MESSAGE until scroll_done + SCROLL_TAIL_MS
SCROLL_TAIL_MS       = 2_000    # extra hold after the scroll completes one pass
ALERT_BEEP_PERIOD_MS = 3_000    # beep every N ms while alert active
ALERT_FLASH_MS       = 200      # generic red alert: how fast red LEDs alternate
MSG_ALERT_FLASH_MS   = 450      # missed-message green alert: 2x slower than red
RX_BLINK_MS          = 80       # blue blink duration on RX
SCROLL_STEP          = 3        # px per frame for message scroll
SCROLL_SCALE         = 2        # font scale for scrolling message
LOG_FILE             = "lora_log.txt"
LOG_RETENTION_DAYS   = 3
MAX_RECENT           = 20       # last N chat messages kept (RAM + flash)
MESSAGES_FILE        = "messages.json"  # persists MAX_RECENT across reboot

# ---- Proximity-driven display ----
# When showing a message, keep it on screen as long as something is closer
# than this. Once distance goes above the threshold, switch back to clock.
PROXIMITY_HOLD_CM    = 65       # cm
PROXIMITY_POLL_MS    = 200      # how often to ping the HC-SR04
# Number of consecutive far readings needed to actually switch to clock,
# so brief glitches/quick movements don't kick you off mid-message.
# 15 readings * 200ms = 3 seconds of "far" before message dismisses.
PROXIMITY_FAR_HITS   = 15
# In ALERT state, getting this close re-shows the latest message
# (same effect as pressing a button).
ALERT_DISMISS_CM     = 15