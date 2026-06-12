"""
display.py - OLED rendering.

The Display class wraps the SSD1306 OLED.
It draws either a clock or a scrolling message.

Usage:
    d = Display(oled)
    d.show_clock()                -- renders HH:MM (one frame)
    d.start_scroll(text, dir=-1)  -- begin scrolling text
    d.scroll_step()               -- render one frame of scroll
"""

import time
import config
import clock_rtc

# Reuse big_text from hardware_test.py
try:
    import hardware_test as ht
    big_text = ht.big_text
except Exception:
    def big_text(*args, **kwargs):
        pass


class Display:
    def __init__(self, oled):
        self.oled = oled
        # scroll state
        self._text = ""
        self._x = 0
        self._dir = -1   # -1 = left, +1 = right
        self._scale = config.SCROLL_SCALE
        # True once the current text has scrolled fully off the screen at
        # least once — used by ui.py to keep MESSAGE state until the user
        # has had a chance to see the whole thing.
        self._completed = False
        # clock cache so we don't redraw every loop tick
        self._last_minute = None

    # ---------- CLOCK ----------
    def show_clock(self, force=False):
        """Render HH:MM at scale 3, vertically centered. Skips redraw if
        same minute as last call (unless force=True)."""
        if self.oled is None:
            return
        h, m, _ = self._read_hm()
        if not force and (h, m) == self._last_minute:
            return
        self._last_minute = (h, m)
        try:
            self.oled.fill(0)
            hm = "{:02d}:{:02d}".format(h, m)
            big_text(self.oled, hm, 4, (config.OLED_HEIGHT - 24) // 2, scale=3)
            self.oled.show()
        except Exception:
            pass

    def _read_hm(self):
        return clock_rtc.now_hm()

    # ---------- SCROLLING MESSAGE ----------
    def start_scroll(self, text, direction=-1):
        """Begin scrolling text. direction: -1 = scroll leftward, +1 = rightward."""
        self._text = text
        self._dir = direction
        text_w = 8 * self._scale * len(text)
        if direction < 0:
            self._x = 128         # start off the right edge, move left
        else:
            self._x = -text_w     # start off the left edge, move right
        self._completed = False   # one full pass not yet done
        self._last_minute = None  # invalidate clock cache

    def _draw_visible(self, text, x, y, scale):
        """Render only the chars of `text` whose bounding box overlaps the
        OLED. big_text() iterates every pixel of the full text, so naive
        rendering of a 100-char message at scale 2 means ~6400 pixel checks
        per frame and big jank on long lines. Clipping to the ~9 visible
        chars at scale 2 keeps per-frame work bounded regardless of length.
        """
        if not text:
            return
        char_w = 8 * scale
        # Char i occupies columns [x + i*char_w, x + (i+1)*char_w - 1].
        # Visible iff that range intersects [0, 127].
        i_min = max(0, (-x) // char_w)
        i_max = min(len(text) - 1, (127 - x) // char_w)
        if i_max < i_min:
            return
        big_text(self.oled, text[i_min:i_max + 1],
                 x + i_min * char_w, y, scale=scale)

    def scroll_step(self):
        """Render one frame of the scroll. Call repeatedly from the main loop."""
        if self.oled is None:
            return
        try:
            self.oled.fill(0)
            self._draw_visible(self._text, self._x,
                               (config.OLED_HEIGHT - 16) // 2, self._scale)
            self.oled.show()
        except Exception:
            pass
        # Advance + wrap. Mark _completed on the first full pass so ui.py
        # can decide when to stop holding MESSAGE state.
        text_w = 8 * self._scale * len(self._text)
        self._x += self._dir * config.SCROLL_STEP
        if self._dir < 0 and self._x < -text_w:
            self._x = 128
            self._completed = True
        elif self._dir > 0 and self._x > 128:
            self._x = -text_w
            self._completed = True

    def scroll_done(self):
        """True once the current text has scrolled fully across the OLED at
        least once."""
        return self._completed

    # ---------- GPS ICON ----------
    def show_gps_icon(self):
        """Draw a GPS navigation arrow centered on the OLED. Full takeover -
        clears the screen and shows just the icon. Caller is responsible for
        re-rendering the clock when the icon should disappear."""
        if self.oled is None:
            return
        try:
            self.oled.fill(0)
            _draw_nav_arrow(self.oled,
                            cx=128 // 2,
                            cy=config.OLED_HEIGHT // 2)
            self.oled.show()
            self._last_minute = None  # invalidate clock cache so it redraws
        except Exception:
            pass


def _draw_nav_arrow(oled, cx, cy):
    """Draw a GPS navigation arrow (pointing right) centered at (cx, cy).

    Shape: tall triangle, wider at the base, with a V-notch cut out of the
    base. The tip points right; the notch is on the left.

    Implementation: vertical scan lines. For each X column from right (tip)
    to left (base), compute top and bottom edges (triangle expanding) and
    the notch (a triangle cut from the left edge growing rightward).
    """
    W = 22
    half_h = 11   # half height at the base
    notch_w = 6   # how deep the V-notch reaches from the left edge

    # The arrow occupies x in [cx - W/2 .. cx + W/2 - 1], with the tip at
    # the rightmost column.
    left = cx - W // 2
    for dx in range(W):
        # dx=0 is the leftmost column (base side); dx=W-1 is the tip.
        # Triangle half-height grows from 0 at the tip to half_h at the base.
        # We want the tip at dx=W-1 with hh=0, base at dx=0 with hh=half_h.
        hh = ((W - 1 - dx) * half_h) // (W - 1)
        y_top    = cy - hh
        y_bottom = cy + hh

        # Notch: only carve from the leftmost notch_w columns.
        # Triangular notch: full depth (full half-height) at dx=0, narrowing
        # to a point at dx=notch_w.
        if dx < notch_w:
            depth_in = notch_w - 1 - dx        # 0 at innermost, notch_w-1 at left edge
            n_hh = (depth_in * (half_h - 2)) // notch_w
            y_notch_top    = cy - n_hh
            y_notch_bottom = cy + n_hh
            # Draw two segments above and below the notch
            if y_top <= y_notch_top - 1:
                h = y_notch_top - y_top
                oled.fill_rect(left + dx, y_top, 1, h, 1)
            if y_notch_bottom + 1 <= y_bottom:
                h = y_bottom - y_notch_bottom
                oled.fill_rect(left + dx, y_notch_bottom + 1, 1, h, 1)
        else:
            h = y_bottom - y_top + 1
            oled.fill_rect(left + dx, y_top, 1, h, 1)


def open_oled():
    """Try to initialize the OLED. Returns the oled object or None on failure."""
    try:
        from machine import I2C, Pin
        from ssd1306 import SSD1306_I2C
        i2c = I2C(0, sda=Pin(config.I2C_SDA), scl=Pin(config.I2C_SCL),
                  freq=400000)
        return SSD1306_I2C(128, config.OLED_HEIGHT, i2c)
    except Exception as e:
        print("LOG:oled_unavailable:{}".format(e))
        return None