from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Sequence, TypeVar

from api.utils.logging import get_logger
from api.utils import xray

BASE_DIR = Path(os.getenv("APP_ROOT", Path(__file__).resolve().parents[2]))
DEFAULT_DB = BASE_DIR / "dialogs.db"
DB_PATH = Path(os.getenv("DATABASE", DEFAULT_DB))
DB_PATH.parent.mkdir(parents=True, exist_ok=True)

logger = get_logger("db")
MIGRATION_ERROR: tuple[Path, Exception] | None = None
T = TypeVar("T")


def _needs_schema_repair(error: sqlite3.OperationalError) -> bool:
    message = str(error).lower()
    if "no such column" not in message and "no column named" not in message:
        return False
    for column in (
        "trial",
        "active",
        "label",
        "payment_url",
        "provider",
        "provider_payment_id",
        "currency",
        "external_status",
        "raw_provider_payload",
        "referrer",
        "source",
        "metadata",
    ):
        if column in message:
            return True
    return False


def _repair_database(error: sqlite3.OperationalError, *, db_path: Path) -> bool:
    message = str(error).lower()
    if "no such table" in message:
        logger.warning(
            "Missing SQLite table detected; reinitialising schema",
            extra={"error": str(error), "path": str(db_path)},
        )
        init_db(db_path=db_path)
        auto_update_missing_fields(db_path=db_path)
        return True

    if _needs_schema_repair(error):
        logger.warning(
            "Database schema mismatch detected; attempting automatic migration",
            extra={"error": str(error), "path": str(db_path)},
        )
        auto_update_missing_fields(db_path=db_path)
        return True

    return False


def _run_with_schema_retry(operation: Callable[[], T], *, db_path: Path | str | None = None) -> T:
    resolved = Path(db_path or DB_PATH)
    try:
        return operation()
    except sqlite3.OperationalError as exc:
        if not _repair_database(exc, db_path=resolved):
            raise
        return operation()

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

CREATE TABLE IF NOT EXISTS history (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER,
  username TEXT,
  full_name TEXT,
  message TEXT,
  reply TEXT,
  created_at TEXT NOT NULL
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
  currency TEXT NOT NULL DEFAULT 'RUB',
  status TEXT NOT NULL,
  paid_at TEXT,
  key_uuid TEXT,
  provider TEXT,
  provider_payment_id TEXT,
  payment_url TEXT,
  external_status TEXT,
  raw_provider_payload TEXT,
  referrer TEXT,
  source TEXT,
  metadata TEXT,
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
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_tg_users_username ON tg_users(username)",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_vpn_keys_uuid ON vpn_keys(uuid)",
    "CREATE INDEX IF NOT EXISTS idx_vpn_keys_username ON vpn_keys(username)",
    "CREATE INDEX IF NOT EXISTS idx_vpn_keys_expires ON vpn_keys(expires_at)",
    "CREATE INDEX IF NOT EXISTS idx_payments_username ON payments(username)",
    "CREATE INDEX IF NOT EXISTS idx_payments_status ON payments(status)",
    "CREATE INDEX IF NOT EXISTS idx_payments_provider_payment_id ON payments(provider_payment_id)",
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


def _apply_indexes(con: sqlite3.Connection) -> None:
    for statement in INDEX_SQL:
        try:
            target = statement.split("ON ", 1)[1].split("(", 1)[0].strip()
            columns_part = statement.split("(", 1)[1].rsplit(")", 1)[0]
            required_columns = {
                column.strip().strip("`\"")
                for column in columns_part.split(",")
                if column.strip()
            }
        except IndexError:  # pragma: no cover - defensive
            continue
        if not _table_exists(con, target):
            continue
        if required_columns and not required_columns.issubset(_table_columns(con, target)):
            logger.debug(
                "Skipping index creation due to missing columns",
                extra={
                    "table": target,
                    "required_columns": sorted(required_columns),
                    "statement": statement,
                },
            )
            continue
        con.execute(statement)


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


def init_db(*, db_path: Path | str | None = None) -> None:
    resolved = Path(db_path or DB_PATH)
    logger.info("Ensuring SQLite schema exists", extra={"path": str(resolved)})
    with connect(db_path=resolved) as con:
        con.executescript(INIT_SQL)
        _apply_indexes(con)
    logger.info("Database initialisation complete", extra={"path": str(resolved)})


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
    def _operation() -> None:
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

    _run_with_schema_retry(_operation)
    logger.info("Stored Telegram user", extra={"username": username, "chat_id": chat_id})


def get_user(username: str) -> dict | None:
    def _operation() -> dict | None:
        with connect() as con:
            cur = con.execute(
                "SELECT * FROM tg_users WHERE username=?",
                (normalise_username(username),),
            )
            row = cur.fetchone()
        return _row_to_dict(row)

    return _run_with_schema_retry(_operation)


def set_user_referrer(username: str, referrer: str) -> None:
    username = normalise_username(username)
    referrer = normalise_username(referrer)
    now = _utcnow().isoformat()
    def _operation() -> None:
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

    _run_with_schema_retry(_operation)
    logger.info("Updated referrer", extra={"username": username, "referrer": referrer})


def get_user_referrer(username: str) -> str | None:
    user = get_user(username)
    if user:
        referrer = user.get("referrer")
        return referrer if referrer else None
    return None


def user_has_trial(username: str) -> bool:
    def _operation() -> bool:
        with connect() as con:
            cur = con.execute(
                "SELECT 1 FROM vpn_keys WHERE username=? AND trial=1",
                (normalise_username(username),),
            )
            row = cur.fetchone()
        return row is not None

    return _run_with_schema_retry(_operation)


def get_active_key(username: str) -> dict | None:
    def _operation() -> dict | None:
        with connect() as con:
            cur = con.execute(
                "SELECT * FROM vpn_keys WHERE username=? AND active=1 ORDER BY expires_at DESC LIMIT 1",
                (normalise_username(username),),
            )
            row = cur.fetchone()
        return _row_to_dict(row)

    return _run_with_schema_retry(_operation)


def get_key_by_uuid(uuid_value: str) -> dict | None:
    def _operation() -> dict | None:
        with connect() as con:
            cur = con.execute("SELECT * FROM vpn_keys WHERE uuid=?", (uuid_value,))
            row = cur.fetchone()
        return _row_to_dict(row)

    return _run_with_schema_retry(_operation)


def list_broadcast_targets() -> list[dict]:
    def _operation() -> list[dict]:
        with connect() as con:
            cur = con.execute(
                """
                SELECT chat_id, username
                FROM tg_users
                WHERE chat_id IS NOT NULL AND chat_id <> 0
                ORDER BY updated_at DESC
                """
            )
            rows = cur.fetchall()
        return [
            {"chat_id": row["chat_id"], "username": row["username"]}
            for row in rows
            if row["chat_id"] is not None
        ]

    return _run_with_schema_retry(_operation)


def list_user_keys(username: str) -> list[dict]:
    def _operation() -> list[dict]:
        with connect() as con:
            cur = con.execute(
                "SELECT * FROM vpn_keys WHERE username=? ORDER BY active DESC, expires_at DESC",
                (normalise_username(username),),
            )
            rows = cur.fetchall()
        return [_row_to_dict(row) for row in rows]

    return _run_with_schema_retry(_operation)


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
    xray_label = (label or username).strip() or username

    def _operation() -> dict:
        with connect() as con:
            cur = con.execute(
                """
                INSERT INTO vpn_keys (username, chat_id, uuid, link, label, country, trial, active, issued_at, expires_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
                """,
                (username, chat_id, uuid_value, link, label, country, int(trial), issued_at, expires_iso),
            )
            key_id = cur.lastrowid
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

    payload = _run_with_schema_retry(_operation)

    try:
        changed = xray.add_client_no_duplicates(uuid_value, xray_label)
    except Exception:
        logger.exception(
            "Failed to sync VPN key with Xray",
            extra={"username": username, "uuid": uuid_value},
        )

        def _rollback() -> None:
            with connect() as con:
                con.execute("DELETE FROM vpn_keys WHERE uuid=?", (uuid_value,))

        _run_with_schema_retry(_rollback)
        raise

    if changed:
        logger.info(
            "Synced VPN key with Xray",
            extra={"username": username, "uuid": uuid_value, "email": xray_label},
        )

    logger.info(
        "Created VPN key",
        extra={"username": username, "uuid": uuid_value, "expires_at": expires_iso, "trial": trial},
    )
    return payload


def update_key_expiry(uuid_value: str, expires_at: datetime) -> None:
    expires_iso = expires_at.replace(microsecond=0).isoformat()

    def _operation() -> None:
        with connect() as con:
            con.execute(
                "UPDATE vpn_keys SET expires_at=?, active=1 WHERE uuid=?",
                (expires_iso, uuid_value),
            )

    _run_with_schema_retry(_operation)
    logger.info("Updated key expiry", extra={"uuid": uuid_value, "expires_at": expires_iso})


def deactivate_key(uuid_value: str) -> None:
    def _operation() -> None:
        with connect() as con:
            con.execute("UPDATE vpn_keys SET active=0 WHERE uuid=?", (uuid_value,))

    _run_with_schema_retry(_operation)
    logger.info("Deactivated VPN key", extra={"uuid": uuid_value})


def _normalise_payment_row(row: dict | None) -> dict | None:
    if row is None:
        return None
    for field in ("raw_provider_payload", "metadata"):
        value = row.get(field)
        if isinstance(value, str) and value:
            try:
                row[field] = json.loads(value)
            except json.JSONDecodeError:
                logger.warning(
                    "Failed to decode JSON column", extra={"field": field, "payment_id": row.get("payment_id")}
                )
    if row.get("referrer") == "":
        row["referrer"] = None
    if row.get("source") == "":
        row["source"] = None
    return row


def create_payment(
    *,
    payment_id: str,
    username: str,
    chat_id: int | None,
    plan: str,
    amount: int,
    currency: str,
    status: str = "pending",
    provider: str | None = None,
    provider_payment_id: str | None = None,
    payment_url: str | None = None,
    external_status: str | None = None,
    raw_provider_payload: dict | None = None,
    referrer: str | None = None,
    source: str | None = None,
    metadata: dict | None = None,
) -> dict:
    now = _utcnow().isoformat()
    username = normalise_username(username)
    referrer_norm = normalise_username(referrer) if referrer else None
    raw_payload_json = json.dumps(raw_provider_payload, ensure_ascii=False) if raw_provider_payload else None
    metadata_json = json.dumps(metadata, ensure_ascii=False) if metadata else None

    def _operation() -> None:
        with connect() as con:
            con.execute(
                """
                INSERT INTO payments (
                    payment_id,
                    username,
                    chat_id,
                    plan,
                    amount,
                    currency,
                    status,
                    paid_at,
                    key_uuid,
                    provider,
                    provider_payment_id,
                    payment_url,
                    external_status,
                    raw_provider_payload,
                    referrer,
                    source,
                    metadata,
                    created_at,
                    updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    payment_id,
                    username,
                    chat_id,
                    plan,
                    amount,
                    currency,
                    status,
                    None,
                    None,
                    provider,
                    provider_payment_id,
                    payment_url,
                    external_status,
                    raw_payload_json,
                    referrer_norm,
                    source,
                    metadata_json,
                    now,
                    now,
                ),
            )

    _run_with_schema_retry(_operation)
    logger.info("Created payment", extra={"payment_id": payment_id, "username": username, "plan": plan})
    return {
        "payment_id": payment_id,
        "username": username,
        "chat_id": chat_id,
        "plan": plan,
        "amount": amount,
        "currency": currency,
        "status": status,
        "provider": provider,
        "provider_payment_id": provider_payment_id,
        "payment_url": payment_url,
        "external_status": external_status,
        "raw_provider_payload": raw_provider_payload,
        "referrer": referrer_norm,
        "source": source,
        "metadata": metadata,
        "created_at": now,
        "updated_at": now,
    }


def update_payment_status(
    payment_id: str,
    *,
    status: str,
    paid_at: datetime | None = None,
    key_uuid: str | None = None,
    provider_status: str | None = None,
    payment_url: str | None = None,
    raw_provider_payload: dict | None = None,
) -> dict | None:
    now = _utcnow().isoformat()
    paid_iso = paid_at.replace(microsecond=0).isoformat() if paid_at else None
    raw_payload_json = json.dumps(raw_provider_payload, ensure_ascii=False) if raw_provider_payload else None

    def _operation() -> dict | None:
        with connect() as con:
            assignments = [
                "status=?",
                "paid_at=COALESCE(?, paid_at)",
                "key_uuid=COALESCE(?, key_uuid)",
                "updated_at=?",
            ]
            params: list[Any] = [status, paid_iso, key_uuid, now]

            if provider_status is not None:
                assignments.append("external_status=?")
                params.append(provider_status)
            if payment_url is not None:
                assignments.append("payment_url=?")
                params.append(payment_url)
            if raw_payload_json is not None:
                assignments.append("raw_provider_payload=?")
                params.append(raw_payload_json)

            params.append(payment_id)

            con.execute(
                f"UPDATE payments SET {', '.join(assignments)} WHERE payment_id=?",
                params,
            )
            cur = con.execute("SELECT * FROM payments WHERE payment_id=?", (payment_id,))
            row = cur.fetchone()
        return _row_to_dict(row)

    result = _run_with_schema_retry(_operation)
    return _normalise_payment_row(result)


def get_payment(payment_id: str) -> dict | None:
    def _operation() -> dict | None:
        with connect() as con:
            cur = con.execute("SELECT * FROM payments WHERE payment_id=?", (payment_id,))
            row = cur.fetchone()
        return _row_to_dict(row)

    result = _run_with_schema_retry(_operation)
    return _normalise_payment_row(result)


def log_referral_bonus(referrer: str, referee: str, bonus_days: int) -> None:
    now = _utcnow().isoformat()
    referrer = normalise_username(referrer)
    referee = normalise_username(referee)
    def _operation() -> None:
        with connect() as con:
            con.execute(
                """
                INSERT OR IGNORE INTO referrals (referrer, referee, bonus_days, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (referrer, referee, bonus_days, now),
            )

    _run_with_schema_retry(_operation)
    logger.info(
        "Recorded referral bonus",
        extra={"referrer": referrer, "referee": referee, "bonus_days": bonus_days},
    )


def list_expiring_keys(*, within_days: int = 3) -> list[dict]:
    cutoff = (_utcnow() + timedelta(days=within_days)).isoformat()
    def _operation() -> list[sqlite3.Row]:
        with connect() as con:
            cur = con.execute(
                """
                SELECT username, chat_id, uuid, expires_at
                FROM vpn_keys
                WHERE active=1 AND expires_at <= ?
                ORDER BY expires_at ASC
                """,
                (cutoff,),
            )
            return cur.fetchall()

    rows = _run_with_schema_retry(_operation)
    now = _utcnow()
    result: list[dict] = []
    for row in rows:
        expires_raw = row["expires_at"]
        try:
            expires_dt = datetime.fromisoformat(expires_raw)
        except Exception:  # pragma: no cover - defensive
            logger.warning(
                "Failed to parse expiry",
                extra={"username": row["username"], "expires_at": expires_raw},
            )
            continue
        remaining = max((expires_dt - now).days, 0)
        result.append(
            {
                "username": row["username"],
                "chat_id": row["chat_id"],
                "uuid": row["uuid"],
                "expires_at": expires_dt.replace(microsecond=0).isoformat(),
                "expires_in_days": remaining,
            }
        )
    return result


def referral_bonus_exists(referrer: str, referee: str) -> bool:
    def _operation() -> bool:
        with connect() as con:
            cur = con.execute(
                "SELECT 1 FROM referrals WHERE referrer=? AND referee=?",
                (normalise_username(referrer), normalise_username(referee)),
            )
            row = cur.fetchone()
        return row is not None

    return _run_with_schema_retry(_operation)


def get_referral_stats(referrer: str) -> dict:
    def _operation() -> dict:
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

    return _run_with_schema_retry(_operation)


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
            if not columns:
                return

            if "uuid" not in columns:
                logger.warning(
                    "Adding missing 'uuid' column to vpn_keys table", extra={"path": str(resolved)}
                )
                con.execute("ALTER TABLE vpn_keys ADD COLUMN uuid TEXT")
                if "key_uuid" in columns:
                    logger.info("Copying legacy key_uuid values into uuid column")
                    con.execute(
                        "UPDATE vpn_keys SET uuid=key_uuid WHERE uuid IS NULL AND key_uuid IS NOT NULL"
                    )

            if "chat_id" not in columns:
                logger.warning(
                    "Adding missing 'chat_id' column to vpn_keys table", extra={"path": str(resolved)}
                )
                con.execute("ALTER TABLE vpn_keys ADD COLUMN chat_id INTEGER")
                if "user_id" in columns:
                    logger.info("Copying legacy user_id values into chat_id column")
                    con.execute(
                        "UPDATE vpn_keys SET chat_id=user_id WHERE chat_id IS NULL AND user_id IS NOT NULL"
                    )

            if "country" not in columns:
                logger.warning(
                    "Adding missing 'country' column to vpn_keys table", extra={"path": str(resolved)}
                )
                con.execute("ALTER TABLE vpn_keys ADD COLUMN country TEXT")

            if "trial" not in columns:
                logger.warning(
                    "Adding missing 'trial' column to vpn_keys table", extra={"path": str(resolved)}
                )
                con.execute(
                    "ALTER TABLE vpn_keys ADD COLUMN trial INTEGER NOT NULL DEFAULT 0"
                )
                logger.info(
                    "Successfully added 'trial' column to vpn_keys table", extra={"path": str(resolved)}
                )

            if "active" not in columns:
                logger.warning(
                    "Adding missing 'active' column to vpn_keys table", extra={"path": str(resolved)}
                )
                con.execute(
                    "ALTER TABLE vpn_keys ADD COLUMN active INTEGER NOT NULL DEFAULT 1"
                )
                logger.info(
                    "Successfully added 'active' column to vpn_keys table", extra={"path": str(resolved)}
                )

            if "label" not in columns:
                logger.warning(
                    "Adding missing 'label' column to vpn_keys table", extra={"path": str(resolved)}
                )
                con.execute("ALTER TABLE vpn_keys ADD COLUMN label TEXT")
                logger.info(
                    "Successfully added 'label' column to vpn_keys table", extra={"path": str(resolved)}
                )

            _apply_indexes(con)

            user_columns = _table_columns(con, "tg_users")
            if user_columns:
                if "referrer" not in user_columns:
                    logger.warning(
                        "Adding missing 'referrer' column to tg_users table", extra={"path": str(resolved)}
                    )
                    con.execute("ALTER TABLE tg_users ADD COLUMN referrer TEXT")

                if "created_at" not in user_columns:
                    logger.warning(
                        "Adding missing 'created_at' column to tg_users table", extra={"path": str(resolved)}
                    )
                    con.execute("ALTER TABLE tg_users ADD COLUMN created_at TEXT")

                if "updated_at" not in user_columns:
                    logger.warning(
                        "Adding missing 'updated_at' column to tg_users table", extra={"path": str(resolved)}
                    )
                    con.execute("ALTER TABLE tg_users ADD COLUMN updated_at TEXT")

            payment_columns = _table_columns(con, "payments")
            if payment_columns:
                if "currency" not in payment_columns:
                    logger.warning(
                        "Adding missing 'currency' column to payments table", extra={"path": str(resolved)}
                    )
                    con.execute("ALTER TABLE payments ADD COLUMN currency TEXT DEFAULT 'RUB'")

                if "provider" not in payment_columns:
                    logger.warning(
                        "Adding missing 'provider' column to payments table", extra={"path": str(resolved)}
                    )
                    con.execute("ALTER TABLE payments ADD COLUMN provider TEXT")

                if "provider_payment_id" not in payment_columns:
                    logger.warning(
                        "Adding missing 'provider_payment_id' column to payments table", extra={"path": str(resolved)}
                    )
                    con.execute("ALTER TABLE payments ADD COLUMN provider_payment_id TEXT")

                if "payment_url" not in payment_columns:
                    logger.warning(
                        "Adding missing 'payment_url' column to payments table", extra={"path": str(resolved)}
                    )
                    con.execute("ALTER TABLE payments ADD COLUMN payment_url TEXT")

                if "external_status" not in payment_columns:
                    logger.warning(
                        "Adding missing 'external_status' column to payments table", extra={"path": str(resolved)}
                    )
                    con.execute("ALTER TABLE payments ADD COLUMN external_status TEXT")

                if "raw_provider_payload" not in payment_columns:
                    logger.warning(
                        "Adding missing 'raw_provider_payload' column to payments table", extra={"path": str(resolved)}
                    )
                    con.execute("ALTER TABLE payments ADD COLUMN raw_provider_payload TEXT")

                if "referrer" not in payment_columns:
                    logger.warning(
                        "Adding missing 'referrer' column to payments table", extra={"path": str(resolved)}
                    )
                    con.execute("ALTER TABLE payments ADD COLUMN referrer TEXT")

                if "source" not in payment_columns:
                    logger.warning(
                        "Adding missing 'source' column to payments table", extra={"path": str(resolved)}
                    )
                    con.execute("ALTER TABLE payments ADD COLUMN source TEXT")

                if "metadata" not in payment_columns:
                    logger.warning(
                        "Adding missing 'metadata' column to payments table", extra={"path": str(resolved)}
                    )
                    con.execute("ALTER TABLE payments ADD COLUMN metadata TEXT")

                _apply_indexes(con)
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
    "list_broadcast_targets",
    "list_user_keys",
    "create_vpn_key",
    "update_key_expiry",
    "deactivate_key",
    "create_payment",
    "get_payment",
    "update_payment_status",
    "log_referral_bonus",
    "list_expiring_keys",
    "referral_bonus_exists",
    "get_referral_stats",
    "extend_active_key",
    "auto_update_missing_fields",
]
