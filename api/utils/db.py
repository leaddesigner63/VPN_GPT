from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterable, Iterator, Sequence

from api.utils.logging import get_logger

BASE_DIR = Path(os.getenv("APP_ROOT", Path(__file__).resolve().parents[2]))
DEFAULT_DB = BASE_DIR / "dialogs.db"
DB_PATH = Path(os.getenv("DATABASE", DEFAULT_DB))
DB_PATH.parent.mkdir(parents=True, exist_ok=True)

logger = get_logger("db")
MIGRATION_ERROR: tuple[Path, Exception] | None = None

INIT_SQL = """
CREATE TABLE IF NOT EXISTS assistant_threads (
  tg_user_id TEXT PRIMARY KEY,
  thread_id  TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tg_users (
  username  TEXT PRIMARY KEY,
  chat_id   INTEGER NOT NULL,
  referrer  TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS vpn_keys (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  username TEXT NOT NULL,
  chat_id INTEGER,
  uuid TEXT NOT NULL UNIQUE,
  link TEXT NOT NULL,
  label TEXT,
  country TEXT,
  trial INTEGER NOT NULL DEFAULT 0,
  active INTEGER NOT NULL DEFAULT 1,
  issued_at TEXT NOT NULL,
  expires_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS payments (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  payment_id TEXT NOT NULL UNIQUE,
  username TEXT NOT NULL,
  chat_id INTEGER,
  plan TEXT NOT NULL,
  amount INTEGER NOT NULL,
  status TEXT NOT NULL,
  paid_at TEXT,
  key_uuid TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS referrals (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  referrer TEXT NOT NULL,
  referee TEXT NOT NULL,
  bonus_days INTEGER NOT NULL,
  created_at TEXT NOT NULL,
  UNIQUE(referrer, referee)
);
"""

INDEX_SQL = (
    "CREATE INDEX IF NOT EXISTS idx_vpn_keys_username ON vpn_keys(username)",
    "CREATE INDEX IF NOT EXISTS idx_vpn_keys_expires ON vpn_keys(expires_at)",
    "CREATE INDEX IF NOT EXISTS idx_payments_username ON payments(username)",
    "CREATE INDEX IF NOT EXISTS idx_payments_status ON payments(status)",
)


def _table_exists(con: sqlite3.Connection, table: str) -> bool:
    cur = con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    )
    return cur.fetchone() is not None


def _table_columns(con: sqlite3.Connection, table: str) -> set[str]:
    if not _table_exists(con, table):
        return set()
    cur = con.execute(f"PRAGMA table_info({table})")
    return {row["name"] for row in cur.fetchall()}


@contextmanager
def connect(*, autocommit: bool = True, db_path: Path | str | None = None) -> Iterator[sqlite3.Connection]:
    resolved = Path(db_path or DB_PATH)
    if MIGRATION_ERROR is not None:
        failed_path, error = MIGRATION_ERROR
        if resolved == failed_path:
            raise RuntimeError("Database migrations failed") from error
    logger.debug("Opening SQLite connection", extra={"path": str(resolved)})
    con = sqlite3.connect(resolved)
    con.row_factory = sqlite3.Row
    try:
        yield con
        if autocommit:
            con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def init_db() -> None:
    logger.info("Ensuring SQLite schema exists", extra={"path": str(DB_PATH)})
    with connect() as con:
        con.executescript(INIT_SQL)
        for statement in INDEX_SQL:
            con.execute(statement)
    logger.info("Database initialisation complete")


def _utcnow() -> datetime:
    return datetime.utcnow().replace(microsecond=0)


def _row_to_dict(row: sqlite3.Row | None) -> dict | None:
    if row is None:
        return None
    return {key: row[key] for key in row.keys()}


def upsert_thread(tg_user_id: str, thread_id: str) -> None:
    now = _utcnow().isoformat()
    with connect() as con:
        con.execute(
            """
            INSERT INTO assistant_threads (tg_user_id, thread_id, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(tg_user_id)
            DO UPDATE SET thread_id=excluded.thread_id, updated_at=excluded.updated_at
            """,
            (tg_user_id, thread_id, now),
        )
    logger.info("Stored assistant thread mapping", extra={"tg_user_id": tg_user_id})


def get_thread(tg_user_id: str) -> str | None:
    with connect() as con:
        cur = con.execute(
            "SELECT thread_id FROM assistant_threads WHERE tg_user_id=?",
            (tg_user_id,),
        )
        row = cur.fetchone()
    if row:
        return row["thread_id"]
    return None


def normalise_username(raw: str | None) -> str:
    if raw is None:
        raise ValueError("username is required")
    username = raw.strip()
    if username.startswith("@"):
        username = username[1:].strip()
    if not username:
        raise ValueError("username is empty")
    return username


def upsert_user(username: str, chat_id: int, *, referrer: str | None = None) -> None:
    username = normalise_username(username)
    now = _utcnow().isoformat()
    with connect() as con:
        con.execute(
            """
            INSERT INTO tg_users (username, chat_id, referrer, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(username)
            DO UPDATE SET
                chat_id=excluded.chat_id,
                updated_at=excluded.updated_at,
                referrer=COALESCE(tg_users.referrer, excluded.referrer)
            """,
            (username, chat_id, referrer, now, now),
        )
    logger.info("Stored Telegram user", extra={"username": username, "chat_id": chat_id})


def get_user(username: str) -> dict | None:
    with connect() as con:
        cur = con.execute("SELECT * FROM tg_users WHERE username=?", (normalise_username(username),))
        row = cur.fetchone()
    return _row_to_dict(row)


def set_user_referrer(username: str, referrer: str) -> None:
    username = normalise_username(username)
    referrer = normalise_username(referrer)
    now = _utcnow().isoformat()
    with connect() as con:
        con.execute(
            """
            INSERT INTO tg_users (username, chat_id, referrer, created_at, updated_at)
            VALUES (?, 0, ?, ?, ?)
            ON CONFLICT(username)
            DO UPDATE SET
                referrer=excluded.referrer,
                updated_at=excluded.updated_at
            """,
            (username, referrer, now, now),
        )
    logger.info("Updated referrer", extra={"username": username, "referrer": referrer})


def get_user_referrer(username: str) -> str | None:
    user = get_user(username)
    if user:
        referrer = user.get("referrer")
        return referrer if referrer else None
    return None


def user_has_trial(username: str) -> bool:
    with connect() as con:
        cur = con.execute(
            "SELECT 1 FROM vpn_keys WHERE username=? AND trial=1",
            (normalise_username(username),),
        )
        row = cur.fetchone()
    return row is not None


def get_active_key(username: str) -> dict | None:
    with connect() as con:
        cur = con.execute(
            "SELECT * FROM vpn_keys WHERE username=? AND active=1 ORDER BY expires_at DESC LIMIT 1",
            (normalise_username(username),),
        )
        row = cur.fetchone()
    return _row_to_dict(row)


def get_key_by_uuid(uuid_value: str) -> dict | None:
    with connect() as con:
        cur = con.execute("SELECT * FROM vpn_keys WHERE uuid=?", (uuid_value,))
        row = cur.fetchone()
    return _row_to_dict(row)


def list_user_keys(username: str) -> list[dict]:
    with connect() as con:
        cur = con.execute(
            "SELECT * FROM vpn_keys WHERE username=? ORDER BY active DESC, expires_at DESC",
            (normalise_username(username),),
        )
        rows = cur.fetchall()
    return [_row_to_dict(row) for row in rows]


def create_vpn_key(
    *,
    username: str,
    chat_id: int | None,
    uuid_value: str,
    link: str,
    expires_at: datetime,
    label: str | None = None,
    country: str | None = None,
    trial: bool = False,
) -> dict:
    username = normalise_username(username)
    issued_at = _utcnow().isoformat()
    expires_iso = expires_at.replace(microsecond=0).isoformat()
    with connect() as con:
        cur = con.execute(
            """
            INSERT INTO vpn_keys (username, chat_id, uuid, link, label, country, trial, active, issued_at, expires_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
            """,
            (username, chat_id, uuid_value, link, label, country, int(trial), issued_at, expires_iso),
        )
        key_id = cur.lastrowid
    logger.info(
        "Created VPN key",
        extra={"username": username, "uuid": uuid_value, "expires_at": expires_iso, "trial": trial},
    )
    return {
        "id": key_id,
        "username": username,
        "chat_id": chat_id,
        "uuid": uuid_value,
        "link": link,
        "label": label,
        "country": country,
        "trial": trial,
        "active": True,
        "issued_at": issued_at,
        "expires_at": expires_iso,
    }


def update_key_expiry(uuid_value: str, expires_at: datetime) -> None:
    expires_iso = expires_at.replace(microsecond=0).isoformat()
    with connect() as con:
        con.execute(
            "UPDATE vpn_keys SET expires_at=?, active=1 WHERE uuid=?",
            (expires_iso, uuid_value),
        )
    logger.info("Updated key expiry", extra={"uuid": uuid_value, "expires_at": expires_iso})


def deactivate_key(uuid_value: str) -> None:
    with connect() as con:
        con.execute("UPDATE vpn_keys SET active=0 WHERE uuid=?", (uuid_value,))
    logger.info("Deactivated VPN key", extra={"uuid": uuid_value})


def create_payment(
    *,
    payment_id: str,
    username: str,
    chat_id: int | None,
    plan: str,
    amount: int,
    status: str = "pending",
) -> dict:
    now = _utcnow().isoformat()
    username = normalise_username(username)
    with connect() as con:
        con.execute(
            """
            INSERT INTO payments (payment_id, username, chat_id, plan, amount, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (payment_id, username, chat_id, plan, amount, status, now, now),
        )
    logger.info("Created payment", extra={"payment_id": payment_id, "username": username, "plan": plan})
    return {
        "payment_id": payment_id,
        "username": username,
        "chat_id": chat_id,
        "plan": plan,
        "amount": amount,
        "status": status,
        "created_at": now,
        "updated_at": now,
    }


def update_payment_status(payment_id: str, *, status: str, paid_at: datetime | None = None, key_uuid: str | None = None) -> dict | None:
    now = _utcnow().isoformat()
    paid_iso = paid_at.replace(microsecond=0).isoformat() if paid_at else None
    with connect() as con:
        con.execute(
            """
            UPDATE payments
            SET status=?, paid_at=COALESCE(?, paid_at), key_uuid=COALESCE(?, key_uuid), updated_at=?
            WHERE payment_id=?
            """,
            (status, paid_iso, key_uuid, now, payment_id),
        )
        cur = con.execute("SELECT * FROM payments WHERE payment_id=?", (payment_id,))
        row = cur.fetchone()
    return _row_to_dict(row)


def get_payment(payment_id: str) -> dict | None:
    with connect() as con:
        cur = con.execute("SELECT * FROM payments WHERE payment_id=?", (payment_id,))
        row = cur.fetchone()
    return _row_to_dict(row)


def log_referral_bonus(referrer: str, referee: str, bonus_days: int) -> None:
    now = _utcnow().isoformat()
    referrer = normalise_username(referrer)
    referee = normalise_username(referee)
    with connect() as con:
        con.execute(
            """
            INSERT OR IGNORE INTO referrals (referrer, referee, bonus_days, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (referrer, referee, bonus_days, now),
        )
    logger.info(
        "Recorded referral bonus",
        extra={"referrer": referrer, "referee": referee, "bonus_days": bonus_days},
    )


def referral_bonus_exists(referrer: str, referee: str) -> bool:
    with connect() as con:
        cur = con.execute(
            "SELECT 1 FROM referrals WHERE referrer=? AND referee=?",
            (normalise_username(referrer), normalise_username(referee)),
        )
        row = cur.fetchone()
    return row is not None


def get_referral_stats(referrer: str) -> dict:
    with connect() as con:
        cur = con.execute(
            """
            SELECT COUNT(*) AS total_referrals, COALESCE(SUM(bonus_days), 0) AS total_days
            FROM referrals
            WHERE referrer=?
            """,
            (normalise_username(referrer),),
        )
        row = cur.fetchone()
    data = _row_to_dict(row) or {"total_referrals": 0, "total_days": 0}
    return data


def extend_active_key(username: str, *, days: int) -> dict | None:
    username = normalise_username(username)
    key = get_active_key(username)
    now = _utcnow()
    if key:
        current_expiry = datetime.fromisoformat(key["expires_at"])
        if current_expiry < now:
            current_expiry = now
        new_expiry = current_expiry + timedelta(days=days)
        update_key_expiry(key["uuid"], new_expiry)
        key["expires_at"] = new_expiry.replace(microsecond=0).isoformat()
        return key
    return None


def auto_update_missing_fields(*, db_path: Path | str | None = None) -> None:  # pragma: no cover - compatibility
    """Apply lightweight migrations to keep backward compatibility with older schemas."""

    resolved = Path(db_path or DB_PATH)
    logger.info("Checking database schema for compatibility", extra={"path": str(resolved)})

    global MIGRATION_ERROR
    try:
        with connect(db_path=resolved) as con:
            columns = _table_columns(con, "vpn_keys")
            if columns and "trial" not in columns:
                logger.warning(
                    "Adding missing 'trial' column to vpn_keys table", extra={"path": str(resolved)}
                )
                con.execute(
                    "ALTER TABLE vpn_keys ADD COLUMN trial INTEGER NOT NULL DEFAULT 0"
                )
                logger.info(
                    "Successfully added 'trial' column to vpn_keys table", extra={"path": str(resolved)}
                )
    except Exception as exc:  # pragma: no cover - defensive
        MIGRATION_ERROR = (resolved, exc)
        logger.exception("Failed to apply database migrations", extra={"path": str(resolved)})
        raise


__all__ = [
    "DB_PATH",
    "connect",
    "init_db",
    "upsert_thread",
    "get_thread",
    "upsert_user",
    "get_user",
    "set_user_referrer",
    "get_user_referrer",
    "user_has_trial",
    "get_active_key",
    "list_user_keys",
    "create_vpn_key",
    "update_key_expiry",
    "deactivate_key",
    "create_payment",
    "get_payment",
    "update_payment_status",
    "log_referral_bonus",
    "referral_bonus_exists",
    "get_referral_stats",
    "extend_active_key",
    "auto_update_missing_fields",
]
