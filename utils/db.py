from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import List, Optional, Tuple


DB_PATH = str(Path(__file__).resolve().parents[1] / "dialogs.db")


def _ensure_db_directory() -> None:
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)


def init_db() -> None:
    _ensure_db_directory()
    with sqlite3.connect(DB_PATH) as conn:
        c = conn.cursor()

        c.execute("""
            CREATE TABLE IF NOT EXISTS history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                username TEXT,
                full_name TEXT,
                message TEXT,
                reply TEXT,
                created_at TEXT
            )
        """)

        c.execute("""
            CREATE TABLE IF NOT EXISTS vpn_keys (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                username TEXT,
                full_name TEXT,
                key_uuid TEXT,
                link TEXT,
                issued_at TEXT,
                expires_at TEXT,
                active INTEGER DEFAULT 1
            )
        """)

        conn.commit()


def save_message(
    user_id: int,
    username: Optional[str],
    full_name: Optional[str],
    message: str,
    reply: str,
) -> None:
    with sqlite3.connect(DB_PATH) as conn:
        c = conn.cursor()
        c.execute(
            """
            INSERT INTO history (user_id, username, full_name, message, reply, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                user_id,
                username,
                full_name,
                message,
                reply,
                datetime.now(UTC).isoformat(),
            ),
        )
        conn.commit()


def get_last_messages(user_id: int, limit: int = 5) -> List[Tuple[str, str]]:
    with sqlite3.connect(DB_PATH) as conn:
        c = conn.cursor()
        c.execute(
            """
            SELECT message, reply FROM history
            WHERE user_id = ?
            ORDER BY id DESC
            LIMIT ?
            """,
            (user_id, limit),
        )
        rows = c.fetchall()
    return list(reversed(rows))


def save_vpn_key(
    user_id: int,
    username: Optional[str],
    full_name: Optional[str],
    link: str,
    expires_at: datetime,
) -> str:
    import uuid

    key_uuid = str(uuid.uuid4())
    with sqlite3.connect(DB_PATH) as conn:
        c = conn.cursor()
        c.execute(
            """
            INSERT INTO vpn_keys (user_id, username, full_name, key_uuid, link, issued_at, expires_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                user_id,
                username,
                full_name,
                key_uuid,
                link,
                datetime.now(UTC).isoformat(),
                expires_at.isoformat(),
            ),
        )
        conn.commit()
    return key_uuid


def get_expiring_keys(days_before: int = 3) -> List[Tuple[int, Optional[str], datetime]]:
    threshold = (datetime.now(UTC) + timedelta(days=days_before)).isoformat()
    with sqlite3.connect(DB_PATH) as conn:
        c = conn.cursor()
        c.execute(
            """
            SELECT user_id, full_name, expires_at FROM vpn_keys
            WHERE active = 1 AND expires_at <= ?
            """,
            (threshold,),
        )
        rows = c.fetchall()

    parsed: List[Tuple[int, Optional[str], datetime]] = []
    for user_id, name, exp in rows:
        try:
            parsed.append((user_id, name, datetime.fromisoformat(exp)))
        except ValueError:
            continue
    return parsed


def renew_vpn_key(user_id: int, extend_days: int = 30) -> Optional[datetime]:
    with sqlite3.connect(DB_PATH) as conn:
        c = conn.cursor()
        c.execute(
            """
            SELECT id, expires_at FROM vpn_keys
            WHERE user_id = ? AND active = 1
            ORDER BY id DESC LIMIT 1
            """,
            (user_id,),
        )
        row = c.fetchone()

        if not row:
            return None

        key_id, old_exp = row
        old_exp_date = datetime.fromisoformat(old_exp)
        new_exp_date = old_exp_date + timedelta(days=extend_days)

        c.execute(
            "UPDATE vpn_keys SET expires_at = ? WHERE id = ?",
            (new_exp_date.isoformat(), key_id),
        )
        conn.commit()
        return new_exp_date


def get_expired_keys() -> List[Tuple[int, Optional[str], str]]:
    now = datetime.now(UTC).isoformat()
    with sqlite3.connect(DB_PATH) as conn:
        c = conn.cursor()
        c.execute(
            """
            SELECT user_id, full_name, link
            FROM vpn_keys
            WHERE active = 1 AND expires_at < ?
            ORDER BY expires_at ASC
            """,
            (now,),
        )
        rows = c.fetchall()
    return rows


def deactivate_vpn_key(user_id: int) -> None:
    with sqlite3.connect(DB_PATH) as conn:
        c = conn.cursor()
        c.execute("UPDATE vpn_keys SET active = 0 WHERE user_id = ?", (user_id,))
        conn.commit()


def get_all_active_users() -> List[Tuple[int, Optional[str], str]]:
    with sqlite3.connect(DB_PATH) as conn:
        c = conn.cursor()
        c.execute(
            """
            SELECT user_id, full_name, expires_at
            FROM vpn_keys
            WHERE active = 1
            ORDER BY expires_at DESC
            """
        )
        rows = c.fetchall()
    return rows

