"""
chat_db.py - SQLite access for the chat message table.

Mirrors the schema in receiver_pi5_advanced.py so hub.py can
read/write the messages table directly without going through the Flask
server. Both processes can have the DB open at the same time -- SQLite
handles short concurrent transactions fine.

Public API:
    db = ChatDB(path)
    db.add_rx(text, rssi=None, snr=None, source='picoB')
    db.add_tx_already_sent(text, source='console')   # for console TX
    db.get_pending_tx(limit=1)                        # rows queued by web
    db.mark_sent(row_id)
    db.mark_failed(row_id, reason)

Self-test runs against a temp file when this module is executed directly.
"""

import os
import sqlite3
from datetime import datetime


_SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    direction TEXT NOT NULL,
    text TEXT NOT NULL,
    recv_at TEXT NOT NULL,
    rssi INTEGER,
    snr REAL,
    source TEXT,
    status TEXT DEFAULT 'sent',
    sent_at TEXT,
    error TEXT
);
CREATE INDEX IF NOT EXISTS idx_messages_id ON messages(id);
CREATE INDEX IF NOT EXISTS idx_messages_status ON messages(status);
"""


class ChatDB:
    def __init__(self, path):
        self.path = path
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self._init_schema()

    def _conn(self):
        # 5s busy timeout in case the Flask process is also writing
        return sqlite3.connect(self.path, timeout=5.0)

    def _init_schema(self):
        conn = self._conn()
        try:
            conn.executescript(_SCHEMA)
            conn.commit()
        finally:
            conn.close()

    # ------------------------------------------------------------
    # Inserts
    # ------------------------------------------------------------
    def add_rx(self, text, rssi=None, snr=None, source='picoB'):
        """Record a message received over LoRa."""
        conn = self._conn()
        try:
            c = conn.cursor()
            c.execute('''INSERT INTO messages
                (direction, text, recv_at, rssi, snr, source, status)
                VALUES ('rx', ?, ?, ?, ?, ?, 'sent')''',
                (text, datetime.now().isoformat(), rssi, snr, source))
            conn.commit()
            return c.lastrowid
        finally:
            conn.close()

    def add_tx_already_sent(self, text, source='console'):
        """Record a TX that we've already sent (e.g. from console)."""
        conn = self._conn()
        try:
            c = conn.cursor()
            now = datetime.now().isoformat()
            c.execute('''INSERT INTO messages
                (direction, text, recv_at, sent_at, source, status)
                VALUES ('tx', ?, ?, ?, ?, 'sent')''',
                (text, now, now, source))
            conn.commit()
            return c.lastrowid
        finally:
            conn.close()

    # ------------------------------------------------------------
    # Outbound queue (web-originated TX waiting to be sent over USB)
    # ------------------------------------------------------------
    def get_pending_tx(self, limit=1):
        """Return [(id, text, source), ...] in FIFO order."""
        conn = self._conn()
        try:
            c = conn.cursor()
            c.execute('''SELECT id, text, source
                         FROM messages
                         WHERE direction='tx' AND status='pending'
                         ORDER BY id ASC
                         LIMIT ?''', (limit,))
            return c.fetchall()
        finally:
            conn.close()

    def mark_sent(self, row_id):
        conn = self._conn()
        try:
            conn.execute(
                "UPDATE messages SET status='sent', sent_at=? WHERE id=?",
                (datetime.now().isoformat(), row_id))
            conn.commit()
        finally:
            conn.close()

    def mark_failed(self, row_id, reason):
        conn = self._conn()
        try:
            conn.execute(
                "UPDATE messages SET status='failed', error=? WHERE id=?",
                (str(reason)[:200], row_id))
            conn.commit()
        finally:
            conn.close()


# ============================================================
# Self-test
# ============================================================
if __name__ == "__main__":
    import tempfile

    print("chat_db.py self-test")
    print("-" * 40)
    failures = 0

    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        path = f.name
    try:
        db = ChatDB(path)

        # 1. RX insert
        rid = db.add_rx("hello world", rssi=-60, snr=9.5)
        ok = (rid == 1)
        print("    add_rx returns id=1                  -> {}".format(
            "OK" if ok else "FAIL"))
        if not ok: failures += 1

        # 2. Console TX insert
        tid = db.add_tx_already_sent("from console", source='console')
        ok = (tid == 2)
        print("    add_tx_already_sent returns id=2     -> {}".format(
            "OK" if ok else "FAIL"))
        if not ok: failures += 1

        # 3. Pending queue is empty
        pending = db.get_pending_tx(limit=10)
        ok = (pending == [])
        print("    no pending after console TX          -> {}".format(
            "OK" if ok else "FAIL"))
        if not ok: failures += 1

        # 4. Simulate the web server inserting a pending row
        conn = sqlite3.connect(path)
        conn.execute('''INSERT INTO messages
            (direction, text, recv_at, source, status)
            VALUES ('tx', 'from web', ?, 'web', 'pending')''',
            (datetime.now().isoformat(),))
        conn.commit()
        conn.close()

        pending = db.get_pending_tx(limit=10)
        ok = (len(pending) == 1 and pending[0][1] == 'from web')
        print("    get_pending_tx returns the row       -> {}".format(
            "OK" if ok else "FAIL"))
        if not ok: failures += 1

        # 5. mark_sent
        db.mark_sent(pending[0][0])
        pending2 = db.get_pending_tx(limit=10)
        ok = (pending2 == [])
        print("    mark_sent clears it from pending     -> {}".format(
            "OK" if ok else "FAIL"))
        if not ok: failures += 1

        # 6. mark_failed
        conn = sqlite3.connect(path)
        conn.execute('''INSERT INTO messages
            (direction, text, recv_at, source, status)
            VALUES ('tx', 'will fail', ?, 'web', 'pending')''',
            (datetime.now().isoformat(),))
        conn.commit()
        conn.close()
        pending3 = db.get_pending_tx(limit=10)
        db.mark_failed(pending3[0][0], "tx_timeout")
        conn = sqlite3.connect(path)
        c = conn.cursor()
        c.execute("SELECT status, error FROM messages WHERE id=?",
                  (pending3[0][0],))
        row = c.fetchone()
        conn.close()
        ok = (row[0] == 'failed' and row[1] == 'tx_timeout')
        print("    mark_failed sets status+error        -> {}".format(
            "OK" if ok else "FAIL"))
        if not ok: failures += 1

    finally:
        os.unlink(path)

    print()
    if failures == 0:
        print("ALL SELF-TESTS PASSED")
    else:
        print("{} FAILURES".format(failures))