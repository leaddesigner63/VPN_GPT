from __future__ import annotations




import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path

from api.utils.logging import get_logger


_BASE_DIR = Path(os.getenv("APP_ROOT", Path(__file__).resolve().parents[2]))
_DEFAULT_DB = _BASE_DIR / "dialogs.db"
DB_PATH = Path(os.getenv("DATABASE", _DEFAULT_DB))
DB_PATH.parent.mkdir(parents=True, exist_ok=True)

logger = get_logger("db")

# Таблицы: history, vpn_keys (есть), добавим assistant_threads (tg_user_id, thread_id)
INIT_SQL = """
CREATE TABLE IF NOT EXISTS assistant_threads (
  tg_user_id TEXT PRIMARY KEY,
  thread_id  TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

-- Стараемся не ломать существующую схему vpn_keys
CREATE TABLE IF NOT EXISTS vpn_keys (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id TEXT,
  username TEXT,
  uuid TEXT,
  link TEXT,
  issued_at TEXT,
  expires_at TEXT,
  active INTEGER DEFAULT 1
);
"""

@contextmanager
def connect():
    logger.debug("Opening SQLite connection to %s", DB_PATH)
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    try:
        yield con
        con.commit()
    finally:
        logger.debug("Closing SQLite connection to %s", DB_PATH)
        con.close()

def init_db():
    logger.info("Ensuring SQLite schema exists at %s", DB_PATH)
    with connect() as con:
        con.executescript(INIT_SQL)
    logger.info("SQLite schema check complete")

def upsert_thread(tg_user_id: str, thread_id: str):
    now = datetime.utcnow().isoformat()
    with connect() as con:
        con.execute("""
        INSERT INTO assistant_threads (tg_user_id, thread_id, updated_at)
        VALUES (?, ?, ?)
        ON CONFLICT(tg_user_id) DO UPDATE SET thread_id=excluded.thread_id, updated_at=excluded.updated_at
        """, (tg_user_id, thread_id, now))
    logger.info("Stored assistant thread mapping", extra={"tg_user_id": tg_user_id})

def get_thread(tg_user_id: str) -> str | None:
    with connect() as con:
        cur = con.execute("SELECT thread_id FROM assistant_threads WHERE tg_user_id=?", (tg_user_id,))
        row = cur.fetchone()
        if row:
            logger.debug("Found thread mapping for user %s", tg_user_id)
            return row["thread_id"]
        logger.debug("No thread mapping for user %s", tg_user_id)
        return None

def get_users(active_only: bool = True):
    with connect() as con:
        if active_only:
            cur = con.execute("SELECT * FROM vpn_keys WHERE active=1")
        else:
            cur = con.execute("SELECT * FROM vpn_keys")
        rows = [dict(r) for r in cur.fetchall()]
        logger.info("Fetched %d users (active_only=%s)", len(rows), active_only)
        return rows

def get_expiring_users(days: int = 3):
    target = (datetime.utcnow() + timedelta(days=days)).date().isoformat()
    with connect() as con:
        cur = con.execute("""
        SELECT * FROM vpn_keys
        WHERE active=1 AND date(expires_at) = date(?)
        """, (target,))
        rows = [dict(r) for r in cur.fetchall()]
        logger.info("Fetched %d users expiring within %d days", len(rows), days)
        return rows

def mark_disabled(uuid: str):
    with connect() as con:
        cur = con.execute("UPDATE vpn_keys SET active=0 WHERE uuid=?", (uuid,))
        if cur.rowcount:
            logger.info("Marked VPN key %s as inactive", uuid)
        else:
            logger.warning("Attempted to deactivate unknown VPN key %s", uuid)

