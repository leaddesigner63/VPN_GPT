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
  active INTEGER DEFAULT 0
);
"""

INDEX_SQL = (
    """
    CREATE UNIQUE INDEX IF NOT EXISTS idx_vpn_keys_username
      ON vpn_keys(username)
      WHERE username IS NOT NULL AND username <> ''
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS idx_vpn_keys_user_id
      ON vpn_keys(user_id)
      WHERE user_id IS NOT NULL AND user_id <> ''
    """,
)

DEDUP_SQL = (
    """
    DELETE FROM vpn_keys
    WHERE username IS NOT NULL
      AND username <> ''
      AND id NOT IN (
        SELECT MAX(id) FROM vpn_keys
        WHERE username IS NOT NULL AND username <> ''
        GROUP BY username
      );
    """,
    """
    DELETE FROM vpn_keys
    WHERE user_id IS NOT NULL
      AND user_id <> ''
      AND id NOT IN (
        SELECT MAX(id) FROM vpn_keys
        WHERE user_id IS NOT NULL AND user_id <> ''
        GROUP BY user_id
      );
    """,
)

@contextmanager
def connect(*, autocommit: bool = True):
    """Return a SQLite connection with optional auto-commit support."""

    logger.debug("Opening SQLite connection to %s", DB_PATH)
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row

    try:
        yield con
        if autocommit:
            con.commit()
    except Exception:
        logger.debug("Rolling back SQLite transaction due to error")
        con.rollback()
        raise
    finally:
        logger.debug("Closing SQLite connection to %s", DB_PATH)
        con.close()

def init_db():
    logger.info("Ensuring SQLite schema exists at %s", DB_PATH)
    with connect() as con:
        con.executescript(INIT_SQL)
        for statement in DEDUP_SQL:
            con.execute(statement)
        for statement in INDEX_SQL:
            con.execute(statement)
        # Normalise empty UUID values to NULL for consistent "no key" semantics.
        con.execute(
            "UPDATE vpn_keys SET uuid=NULL WHERE uuid IS NOT NULL AND TRIM(uuid)=''"
        )
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


def _row_to_dict(row: sqlite3.Row | None) -> dict | None:
    if row is None:
        return None
    return {key: row[key] for key in row.keys()}


def _safe_fetch_one(con: sqlite3.Connection, query: str, params: tuple) -> sqlite3.Row | None:
    try:
        cur = con.execute(query, params)
    except sqlite3.OperationalError as exc:
        logger.debug("Query failed (ignored)", extra={"query": query, "error": str(exc)})
        return None
    return cur.fetchone()


def get_vpn_user_full(username: str) -> dict | None:
    """Return merged VPN data enriched with Telegram and history records."""

    with connect() as con:
        cur = con.execute(
            "SELECT * FROM vpn_keys WHERE username=? AND active=1",
            (username,),
        )
        vpn_row = cur.fetchone()
        if vpn_row is None:
            logger.info("VPN user not found or inactive", extra={"username": username})
            return None

        vpn_data = _row_to_dict(vpn_row) or {}

        if not vpn_data.get("chat_id"):
            chat_row = _safe_fetch_one(
                con,
                "SELECT chat_id FROM tg_users WHERE username=?",
                (username,),
            )
            if chat_row and chat_row["chat_id"] is not None:
                vpn_data["chat_id"] = chat_row["chat_id"]

        if not vpn_data.get("user_id"):
            user_id_value = None

            history_row = _safe_fetch_one(
                con,
                """
                SELECT user_id
                FROM history
                WHERE username=? AND user_id IS NOT NULL AND user_id <> ''
                ORDER BY id DESC
                LIMIT 1
                """,
                (username,),
            )
            if history_row and history_row["user_id"]:
                user_id_value = history_row["user_id"]
            elif vpn_data.get("username"):
                user_id_value = vpn_data["username"]

            if user_id_value:
                vpn_data["user_id"] = user_id_value

        return vpn_data


def auto_update_missing_fields():
    """Fill missing identifiers in ``vpn_keys`` table from related tables."""

    with connect() as con:
        # Auto-fill chat_id values
        cur = con.execute(
            """
            SELECT vk.id, vk.username
            FROM vpn_keys AS vk
            WHERE vk.chat_id IS NULL OR vk.chat_id = ''
            """
        )
        for row in cur.fetchall():
            chat_row = _safe_fetch_one(
                con,
                "SELECT chat_id FROM tg_users WHERE username=?",
                (row["username"],),
            )
            if chat_row and chat_row["chat_id"] is not None:
                con.execute(
                    "UPDATE vpn_keys SET chat_id=? WHERE id=?",
                    (chat_row["chat_id"], row["id"]),
                )
                logger.info(
                    "Auto-filled chat_id for user %s", row["username"]
                )

        # Auto-fill user_id values
        cur = con.execute(
            """
            SELECT vk.id, vk.username
            FROM vpn_keys AS vk
            WHERE vk.user_id IS NULL OR vk.user_id = ''
            """
        )
        for row in cur.fetchall():
            user_id_value = None

            history_row = _safe_fetch_one(
                con,
                """
                SELECT user_id
                FROM history
                WHERE username=? AND user_id IS NOT NULL AND user_id <> ''
                ORDER BY id DESC
                LIMIT 1
                """,
                (row["username"],),
            )
            if history_row and history_row["user_id"]:
                user_id_value = history_row["user_id"]
            elif row["username"]:
                user_id_value = row["username"]

            if user_id_value:
                con.execute(
                    "UPDATE vpn_keys SET user_id=? WHERE id=?",
                    (user_id_value, row["id"]),
                )
                logger.info(
                    "Auto-filled user_id for user %s", row["username"]
                )

