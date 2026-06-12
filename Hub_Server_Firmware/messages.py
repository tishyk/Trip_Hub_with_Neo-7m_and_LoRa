"""
messages.py - rolling buffer of the last N received messages.

Persisted to flash (config.MESSAGES_FILE) so the last MAX_RECENT chat
messages survive a Pico A reboot. storage.py remains the separate
long-form event log.

Browsing API:
    add(text, rssi, snr)    -- store a new message (resets cursor to latest)
    latest()                -- (ts, text, rssi, snr) of newest, or None
    older() / newer()       -- step the browse cursor and return current
    current()               -- the message at the current cursor position
"""

import json
import os

import config
import clock_rtc


class MessageBuffer:
    def __init__(self, n=None):
        self.n = n or config.MAX_RECENT
        self.items = []   # list of (timestamp_str, text, rssi, snr)
        # browse cursor: index into self.items.
        # -1 means "no items" or "show latest" (auto-tracks newest).
        # When the user starts navigating, this becomes a real index.
        self._cursor = None  # None = show latest
        self._load()

    def add(self, text, rssi, snr):
        self.items.append((clock_rtc.now_str(), text, rssi, snr))
        if len(self.items) > self.n:
            self.items = self.items[-self.n:]
        # New message arrives -> jump back to latest
        self._cursor = None
        self._save()

    # ---- flash persistence -------------------------------------------------
    def _load(self):
        """Restore items from flash. Missing/corrupt file -> start empty."""
        try:
            with open(config.MESSAGES_FILE) as f:
                data = json.load(f)
        except (OSError, ValueError):
            return
        if not isinstance(data, list):
            return
        items = []
        for it in data:
            if isinstance(it, (list, tuple)) and len(it) >= 4:
                items.append((it[0], it[1], it[2], it[3]))
        self.items = items[-self.n:]

    def _save(self):
        """Atomic tmp+rename write so a power loss mid-write can't corrupt
        the file (same pattern PicoB uses for sync_state.json)."""
        tmp = config.MESSAGES_FILE + ".tmp"
        try:
            with open(tmp, "w") as f:
                json.dump(self.items, f)
        except OSError as e:
            print("LOG:msg_save_failed:{}".format(e))
            return
        try:
            os.rename(tmp, config.MESSAGES_FILE)
        except OSError:
            # Some MicroPython ports won't rename onto an existing file;
            # fall back to a direct overwrite (loses only atomicity).
            try:
                with open(config.MESSAGES_FILE, "w") as f:
                    json.dump(self.items, f)
                os.remove(tmp)
            except OSError:
                pass

    def latest(self):
        return self.items[-1] if self.items else None

    def current(self):
        """The item the browse cursor points at. None if buffer empty."""
        if not self.items:
            return None
        if self._cursor is None:
            return self.items[-1]
        # Clamp cursor in case items were pruned
        if self._cursor < 0:
            self._cursor = 0
        elif self._cursor >= len(self.items):
            self._cursor = len(self.items) - 1
        return self.items[self._cursor]

    def older(self):
        """Step cursor back (older). Returns the item now under cursor."""
        if not self.items:
            return None
        if self._cursor is None:
            # First press: start from latest, step back one
            self._cursor = len(self.items) - 1
        if self._cursor > 0:
            self._cursor -= 1
        return self.items[self._cursor]

    def newer(self):
        """Step cursor forward (newer). Returns the item now under cursor."""
        if not self.items:
            return None
        if self._cursor is None:
            # Already at latest
            return self.items[-1]
        if self._cursor < len(self.items) - 1:
            self._cursor += 1
        return self.items[self._cursor]

    def position(self):
        """Returns (index_from_newest, total). e.g. (0, 5) means showing newest of 5.
        (4, 5) means showing oldest of 5."""
        if not self.items:
            return (0, 0)
        cur = self._cursor if self._cursor is not None else len(self.items) - 1
        from_newest = (len(self.items) - 1) - cur
        return (from_newest, len(self.items))

    def __len__(self):
        return len(self.items)