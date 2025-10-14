from __future__ import annotations

from datetime import datetime, timedelta
from typing import Iterable, List, Tuple

from api.utils import db as core_db

DB_PATH = str(core_db.DB_PATH)


def init_db() -> None:
    """Initialise the shared SQLite database and ensure legacy tables exist."""
    core_db.init_db()
    with core_db.connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                username TEXT,
                full_name TEXT,
                message TEXT,
                reply TEXT,
                created_at TEXT NOT NULL
            )
            """
        )


def save_message(
    user_id: int | None,
    username: str | None,
    full_name: str | None,
    message: str,
    reply: str,
) -> None:
    """Persist a message/reply exchange for conversational history."""
    created_at = datetime.utcnow().replace(microsecond=0).isoformat()
    with core_db.connect() as conn:
        conn.execute(
            """
            INSERT INTO history (user_id, username, full_name, message, reply, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (user_id, username, full_name, message, reply, created_at),
        )


def get_last_messages(user_id: int, limit: int = 5) -> List[Tuple[str, str]]:
    """Return the most recent message/reply pairs for the given user."""
    with core_db.connect() as conn:
        cur = conn.execute(
            """
            SELECT message, reply FROM history
            WHERE user_id = ?
            ORDER BY id DESC
            LIMIT ?
            """,
            (user_id, limit),
        )
        rows = cur.fetchall()
    return [(row["message"], row["reply"]) for row in reversed(rows)]


def _coerce_datetime(value: datetime | str) -> datetime:
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(value)


def save_vpn_key(
    user_id: int | None,
    username: str,
    full_name: str | None,
    link: str,
    expires_at: datetime | str,
) -> str | None:
    """Persist a newly issued VPN key if the user does not have an active one."""
    from uuid import uuid4

    normalised_username = core_db.normalise_username(username)
    expires_dt = _coerce_datetime(expires_at).replace(microsecond=0)

    with core_db.connect() as conn:
        cur = conn.execute(
            """
            SELECT uuid, active FROM vpn_keys
            WHERE (username = ? AND username IS NOT NULL)
               OR (chat_id = ? AND chat_id IS NOT NULL)
            ORDER BY active DESC, expires_at DESC
            LIMIT 1
            """,
            (normalised_username, user_id),
        )
        existing = cur.fetchone()
        if existing and existing["active"]:
            return None

    key_uuid = str(uuid4())
    label = full_name or f"VPN_GPT_{normalised_username}"
    payload = core_db.create_vpn_key(
        username=normalised_username,
        chat_id=user_id,
        uuid_value=key_uuid,
        link=link,
        expires_at=expires_dt,
        label=label,
        country=None,
        trial=False,
    )
    return payload["uuid"]


def get_expiring_keys(days_before: int = 3) -> List[Tuple[int | None, str, datetime]]:
    """Return chat identifiers and expiry dates for keys ending soon."""
    records = core_db.list_expiring_keys(within_days=days_before)
    result: list[Tuple[int | None, str, datetime]] = []
    for record in records:
        try:
            expires = datetime.fromisoformat(record["expires_at"])
        except Exception:
            continue
        result.append((record.get("chat_id"), record["username"], expires))
    return result


def renew_vpn_key(user_id: int, extend_days: int = 30) -> datetime | None:
    """Extend the expiry for the most recent active key linked to the chat."""
    with core_db.connect() as conn:
        cur = conn.execute(
            """
            SELECT uuid, expires_at FROM vpn_keys
            WHERE chat_id = ? AND active = 1
            ORDER BY expires_at DESC
            LIMIT 1
            """,
            (user_id,),
        )
        row = cur.fetchone()

    if not row:
        return None

    try:
        current_expiry = datetime.fromisoformat(row["expires_at"])
    except Exception:
        current_expiry = datetime.utcnow()

    if current_expiry < datetime.utcnow():
        current_expiry = datetime.utcnow()

    new_expiry = current_expiry + timedelta(days=extend_days)
    core_db.update_key_expiry(row["uuid"], new_expiry)
    return new_expiry


def get_expired_keys() -> List[Tuple[int | None, str, str]]:
    """Return all active keys that are already past their expiry date."""
    now_iso = datetime.utcnow().replace(microsecond=0).isoformat()
    with core_db.connect() as conn:
        cur = conn.execute(
            """
            SELECT chat_id, username, link
            FROM vpn_keys
            WHERE active = 1 AND expires_at < ?
            ORDER BY expires_at ASC
            """,
            (now_iso,),
        )
        rows = cur.fetchall()
    return [(row["chat_id"], row["username"], row["link"]) for row in rows]


def deactivate_vpn_key(user_id: int) -> None:
    """Mark the VPN key linked to the chat identifier as inactive."""
    with core_db.connect() as conn:
        conn.execute("UPDATE vpn_keys SET active = 0 WHERE chat_id = ?", (user_id,))


def get_all_active_users() -> List[Tuple[int | None, str, str]]:
    """Return all active subscriptions ordered by expiry date."""
    with core_db.connect() as conn:
        cur = conn.execute(
            """
            SELECT chat_id, username, expires_at
            FROM vpn_keys
            WHERE active = 1
            ORDER BY expires_at DESC
            """
        )
        rows = cur.fetchall()
    return [(row["chat_id"], row["username"], row["expires_at"]) for row in rows]


__all__: Iterable[str] = (
    "DB_PATH",
    "init_db",
    "save_message",
    "get_last_messages",
    "save_vpn_key",
    "get_expiring_keys",
    "renew_vpn_key",
    "get_expired_keys",
    "deactivate_vpn_key",
    "get_all_active_users",
)
