import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path


_BASE_DIR = Path(os.getenv("APP_ROOT", Path(__file__).resolve().parents[2]))
_DEFAULT_DB = _BASE_DIR / "dialogs.db"
DB_PATH = Path(os.getenv("DATABASE", _DEFAULT_DB))
DB_PATH.parent.mkdir(parents=True, exist_ok=True)

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
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    try:
        yield con
        con.commit()
    finally:
        con.close()

def init_db():
    with connect() as con:
        con.executescript(INIT_SQL)

def upsert_thread(tg_user_id: str, thread_id: str):
    now = datetime.utcnow().isoformat()
    with connect() as con:
        con.execute("""
        INSERT INTO assistant_threads (tg_user_id, thread_id, updated_at)
        VALUES (?, ?, ?)
        ON CONFLICT(tg_user_id) DO UPDATE SET thread_id=excluded.thread_id, updated_at=excluded.updated_at
        """, (tg_user_id, thread_id, now))

def get_thread(tg_user_id: str) -> str | None:
    with connect() as con:
        cur = con.execute("SELECT thread_id FROM assistant_threads WHERE tg_user_id=?", (tg_user_id,))
        row = cur.fetchone()
        return row["thread_id"] if row else None

def get_users(active_only: bool = True):
    with connect() as con:
        if active_only:
            cur = con.execute("SELECT * FROM vpn_keys WHERE active=1")
        else:
            cur = con.execute("SELECT * FROM vpn_keys")
        return [dict(r) for r in cur.fetchall()]

def get_expiring_users(days: int = 3):
    target = (datetime.utcnow() + timedelta(days=days)).date().isoformat()
    with connect() as con:
        cur = con.execute("""
        SELECT * FROM vpn_keys
        WHERE active=1 AND date(expires_at) = date(?)
        """, (target,))
        return [dict(r) for r in cur.fetchall()]

def mark_disabled(uuid: str):
    with connect() as con:
        con.execute("UPDATE vpn_keys SET active=0 WHERE uuid=?", (uuid,))

