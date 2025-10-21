from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
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

RENEWAL_NOTIFICATION_STAGE_COUNT = 1
_DEFAULT_NOTIFICATION_INTERVAL_HOURS = 24.0
_DEFAULT_NOTIFICATION_RETRY_HOURS = 1.0

_STAR_PAYMENT_ALLOWED_STATUSES = {"paid", "refunded", "canceled", "failed"}


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
  is_subscription INTEGER NOT NULL DEFAULT 0,
  active INTEGER NOT NULL DEFAULT 1,
  issued_at TEXT NOT NULL,
  expires_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS payments (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  payment_id TEXT NOT NULL UNIQUE,
  order_id TEXT NOT NULL UNIQUE,
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

CREATE TABLE IF NOT EXISTS renewal_notifications (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  key_uuid TEXT NOT NULL UNIQUE,
  username TEXT,
  chat_id INTEGER,
  expires_at TEXT,
  stage INTEGER NOT NULL DEFAULT 0,
  last_sent_at TEXT,
  next_attempt_at TEXT NOT NULL,
  completed INTEGER NOT NULL DEFAULT 0,
  last_error TEXT,
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

CREATE TABLE IF NOT EXISTS star_payments (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL,
  username TEXT,
  plan TEXT NOT NULL,
  amount_stars INTEGER NOT NULL,
  charge_id TEXT,
  is_subscription INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL,
  paid_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  refunded_at TIMESTAMP,
  fulfilled_at TIMESTAMP,
  delivery_pending INTEGER NOT NULL DEFAULT 0,
  delivery_attempts INTEGER NOT NULL DEFAULT 0,
  last_delivery_attempt TIMESTAMP,
  delivery_error TEXT
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
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_payments_order_id ON payments(order_id)",
    "CREATE INDEX IF NOT EXISTS idx_renewal_notifications_next_attempt ON renewal_notifications(next_attempt_at)",
    "CREATE INDEX IF NOT EXISTS idx_star_payments_user ON star_payments(user_id)",
    "CREATE INDEX IF NOT EXISTS idx_star_payments_charge ON star_payments(charge_id)",
    "CREATE INDEX IF NOT EXISTS idx_star_payments_status ON star_payments(status)",
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
    return datetime.now(UTC).replace(microsecond=0)


def _ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _row_to_dict(row: sqlite3.Row | None) -> dict | None:
    if row is None:
        return None
    return {key: row[key] for key in row.keys()}


def _normalise_key_row(row: dict | None) -> dict | None:
    if row is None:
        return None
    result = dict(row)
    result["trial"] = bool(result.get("trial"))
    result["active"] = bool(result.get("active"))
    result["is_subscription"] = bool(result.get("is_subscription"))
    return result


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
        return _normalise_key_row(_row_to_dict(row))

    return _run_with_schema_retry(_operation)


def get_key_by_uuid(uuid_value: str) -> dict | None:
    def _operation() -> dict | None:
        with connect() as con:
            cur = con.execute("SELECT * FROM vpn_keys WHERE uuid=?", (uuid_value,))
            row = cur.fetchone()
        return _normalise_key_row(_row_to_dict(row))

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


def get_users_summary() -> list[dict]:
    """Return aggregated statistics for all known Telegram users."""

    def _operation() -> list[dict]:
        with connect() as con:
            con.row_factory = sqlite3.Row
            cur = con.execute(
                """
                WITH key_stats AS (
                    SELECT
                        username,
                        COUNT(*) AS total_keys,
                        SUM(CASE WHEN active = 1 THEN 1 ELSE 0 END) AS active_keys,
                        MAX(CASE WHEN trial = 1 THEN 1 ELSE 0 END) AS has_trial_key,
                        MAX(issued_at) AS last_key_issued_at,
                        MAX(expires_at) AS last_key_expires_at
                    FROM vpn_keys
                    GROUP BY username
                ),
                payment_stats AS (
                    SELECT
                        username,
                        COUNT(*) AS total_payments,
                        SUM(CASE WHEN status = 'paid' THEN 1 ELSE 0 END) AS paid_payments,
                        SUM(CASE WHEN status = 'paid' THEN amount ELSE 0 END) AS paid_amount,
                        MAX(CASE WHEN status = 'paid' THEN paid_at ELSE NULL END) AS last_payment_at
                    FROM payments
                    GROUP BY username
                )
                SELECT
                    u.username,
                    u.chat_id,
                    u.referrer,
                    u.created_at,
                    u.updated_at,
                    COALESCE(k.total_keys, 0) AS total_keys,
                    COALESCE(k.active_keys, 0) AS active_keys,
                    COALESCE(k.has_trial_key, 0) AS has_trial_key,
                    k.last_key_issued_at,
                    k.last_key_expires_at,
                    COALESCE(p.total_payments, 0) AS total_payments,
                    COALESCE(p.paid_payments, 0) AS paid_payments,
                    COALESCE(p.paid_amount, 0) AS paid_amount,
                    p.last_payment_at
                FROM tg_users AS u
                LEFT JOIN key_stats AS k ON k.username = u.username
                LEFT JOIN payment_stats AS p ON p.username = u.username
                ORDER BY u.updated_at DESC
                """
            )
            rows = cur.fetchall()

        summary: list[dict] = []
        for row in rows:
            chat_id = row["chat_id"]
            referrer = row["referrer"] or None
            summary.append(
                {
                    "username": row["username"],
                    "chat_id": int(chat_id) if chat_id not in (None, 0) else None,
                    "referrer": referrer,
                    "created_at": row["created_at"],
                    "updated_at": row["updated_at"],
                    "total_keys": int(row["total_keys"] or 0),
                    "active_keys": int(row["active_keys"] or 0),
                    "has_trial_key": bool(row["has_trial_key"]),
                    "last_key_issued_at": row["last_key_issued_at"],
                    "last_key_expires_at": row["last_key_expires_at"],
                    "total_payments": int(row["total_payments"] or 0),
                    "paid_payments": int(row["paid_payments"] or 0),
                    "paid_amount": int(row["paid_amount"] or 0),
                    "last_payment_at": row["last_payment_at"],
                }
            )

        return summary

    return _run_with_schema_retry(_operation)


def list_user_keys(username: str) -> list[dict]:
    def _operation() -> list[dict]:
        with connect() as con:
            cur = con.execute(
                "SELECT * FROM vpn_keys WHERE username=? ORDER BY active DESC, expires_at DESC",
                (normalise_username(username),),
            )
            rows = cur.fetchall()
        return [_normalise_key_row(_row_to_dict(row)) or {} for row in rows]

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
    is_subscription: bool = False,
) -> dict:
    username = normalise_username(username)
    issued_at = _utcnow().isoformat()
    expires_iso = expires_at.replace(microsecond=0).isoformat()
    xray_label = (label or username).strip() or username

    def _operation() -> dict:
        with connect() as con:
            cur = con.execute(
                """
                INSERT INTO vpn_keys (username, chat_id, uuid, link, label, country, trial, is_subscription, active, issued_at, expires_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
                """,
                (
                    username,
                    chat_id,
                    uuid_value,
                    link,
                    label,
                    country,
                    int(trial),
                    1 if is_subscription else 0,
                    issued_at,
                    expires_iso,
                ),
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
            "trial": bool(trial),
            "is_subscription": bool(is_subscription),
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


def update_key_expiry(
    uuid_value: str, expires_at: datetime, *, is_subscription: bool | None = None
) -> None:
    expires_iso = expires_at.replace(microsecond=0).isoformat()

    def _operation() -> None:
        with connect() as con:
            if is_subscription is None:
                con.execute(
                    "UPDATE vpn_keys SET expires_at=?, active=1 WHERE uuid=?",
                    (expires_iso, uuid_value),
                )
            else:
                con.execute(
                    "UPDATE vpn_keys SET expires_at=?, active=1, is_subscription=? WHERE uuid=?",
                    (expires_iso, 1 if is_subscription else 0, uuid_value),
                )

    _run_with_schema_retry(_operation)
    logger.info(
        "Updated key expiry",
        extra={
            "uuid": uuid_value,
            "expires_at": expires_iso,
            "is_subscription": is_subscription,
        },
    )


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


def _normalise_star_payment_row(row: dict | None) -> dict | None:
    if row is None:
        return None
    result = dict(row)
    username = result.get("username")
    if username:
        try:
            result["username"] = normalise_username(username)
        except ValueError:
            pass
    result["is_subscription"] = bool(result.get("is_subscription"))
    result["delivery_pending"] = bool(result.get("delivery_pending"))
    result["amount_stars"] = int(result.get("amount_stars", 0) or 0)
    result["delivery_attempts"] = int(result.get("delivery_attempts", 0) or 0)
    status = (result.get("status") or "").strip()
    if status and status not in _STAR_PAYMENT_ALLOWED_STATUSES:
        logger.warning(
            "Unknown star payment status encountered", extra={"status": status, "id": result.get("id")}
        )
    return result


def create_payment(
    *,
    payment_id: str,
    order_id: str | None,
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
    resolved_order_id = (order_id or payment_id).strip()
    if not resolved_order_id:
        raise ValueError("order_id is required")

    def _operation() -> None:
        with connect() as con:
            con.execute(
                """
                INSERT INTO payments (
                    payment_id,
                    order_id,
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
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                ( 
                    payment_id,
                    resolved_order_id,
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
        "order_id": resolved_order_id,
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
    provider_payment_id: str | None = None,
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
            if provider_payment_id is not None:
                assignments.append("provider_payment_id=?")
                params.append(provider_payment_id)

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


def get_payment_by_order(order_id: str) -> dict | None:
    def _operation() -> dict | None:
        with connect() as con:
            cur = con.execute("SELECT * FROM payments WHERE order_id=?", (order_id,))
            row = cur.fetchone()
        return _row_to_dict(row)

    result = _run_with_schema_retry(_operation)
    return _normalise_payment_row(result)


def _coerce_star_status(status: str) -> str:
    cleaned = (status or "").strip().lower()
    if cleaned not in _STAR_PAYMENT_ALLOWED_STATUSES:
        raise ValueError(f"Invalid star payment status: {status}")
    return cleaned


def create_star_payment(
    *,
    user_id: int,
    username: str | None,
    plan: str,
    amount_stars: int,
    charge_id: str | None,
    is_subscription: bool = False,
    status: str = "paid",
    delivery_pending: bool = False,
    refunded_at: datetime | None = None,
) -> dict:
    status_clean = _coerce_star_status(status)
    normalized_username: str | None = None
    if username:
        try:
            normalized_username = normalise_username(username)
        except ValueError:
            normalized_username = username.strip()

    if charge_id:
        existing = get_star_payment_by_charge(charge_id)
        if existing:
            return existing

    paid_iso = _utcnow().replace(microsecond=0).isoformat()
    refunded_iso = refunded_at.replace(microsecond=0).isoformat() if refunded_at else None

    def _operation() -> dict:
        with connect() as con:
            con.execute(
                """
                INSERT INTO star_payments (
                    user_id,
                    username,
                    plan,
                    amount_stars,
                    charge_id,
                    is_subscription,
                    status,
                    paid_at,
                    refunded_at,
                    fulfilled_at,
                    delivery_pending,
                    delivery_attempts,
                    last_delivery_attempt,
                    delivery_error
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, 0, NULL, NULL)
                """,
                (
                    int(user_id),
                    normalized_username,
                    plan,
                    int(amount_stars),
                    charge_id,
                    1 if is_subscription else 0,
                    status_clean,
                    paid_iso,
                    refunded_iso,
                    1 if delivery_pending else 0,
                ),
            )
            cur = con.execute("SELECT * FROM star_payments WHERE id=last_insert_rowid()")
            row = cur.fetchone()
        return _row_to_dict(row) or {}

    row = _run_with_schema_retry(_operation)
    return _normalise_star_payment_row(row) or {}


def get_star_payment(payment_id: int) -> dict | None:
    def _operation() -> dict | None:
        with connect() as con:
            cur = con.execute("SELECT * FROM star_payments WHERE id=?", (payment_id,))
            return _row_to_dict(cur.fetchone())

    result = _run_with_schema_retry(_operation)
    return _normalise_star_payment_row(result)


def get_star_payment_by_charge(charge_id: str) -> dict | None:
    if not charge_id:
        return None

    def _operation() -> dict | None:
        with connect() as con:
            cur = con.execute("SELECT * FROM star_payments WHERE charge_id=?", (charge_id,))
            return _row_to_dict(cur.fetchone())

    result = _run_with_schema_retry(_operation)
    return _normalise_star_payment_row(result)


def update_star_payment_status(
    payment_id: int,
    *,
    status: str | None = None,
    refunded_at: datetime | None = None,
    fulfilled_at: datetime | None = None,
    delivery_pending: bool | None = None,
    delivery_error: str | None = None,
    charge_id: str | None = None,
) -> dict | None:
    if status is not None:
        status = _coerce_star_status(status)
    refunded_iso = refunded_at.replace(microsecond=0).isoformat() if refunded_at else None
    fulfilled_iso = fulfilled_at.replace(microsecond=0).isoformat() if fulfilled_at else None
    delivery_attempts_increment = 1 if delivery_pending else 0
    now_iso = _utcnow().replace(microsecond=0).isoformat()

    def _operation() -> dict | None:
        with connect() as con:
            assignments: list[str] = []
            params: list[Any] = []
            if status is not None:
                assignments.append("status=?")
                params.append(status)
            if refunded_at is not None:
                assignments.append("refunded_at=?")
                params.append(refunded_iso)
            if fulfilled_at is not None:
                assignments.append("fulfilled_at=?")
                params.append(fulfilled_iso)
            if delivery_pending is not None:
                assignments.append("delivery_pending=?")
                params.append(1 if delivery_pending else 0)
                assignments.append("last_delivery_attempt=?")
                params.append(now_iso)
                if delivery_attempts_increment:
                    assignments.append("delivery_attempts = delivery_attempts + 1")
            if delivery_error is not None:
                assignments.append("delivery_error=?")
                params.append(delivery_error[:500] if delivery_error else None)
            if charge_id is not None:
                assignments.append("charge_id=?")
                params.append(charge_id)

            if not assignments:
                cur = con.execute("SELECT * FROM star_payments WHERE id=?", (payment_id,))
                return _row_to_dict(cur.fetchone())

            params.append(payment_id)
            query = f"UPDATE star_payments SET {', '.join(assignments)} WHERE id=?"
            con.execute(query, params)
            cur = con.execute("SELECT * FROM star_payments WHERE id=?", (payment_id,))
            return _row_to_dict(cur.fetchone())

    result = _run_with_schema_retry(_operation)
    return _normalise_star_payment_row(result)


def list_pending_star_payments(username: str) -> list[dict]:
    if not username:
        return []
    try:
        normalized = normalise_username(username)
    except ValueError:
        normalized = username

    def _operation() -> list[sqlite3.Row]:
        with connect() as con:
            cur = con.execute(
                """
                SELECT * FROM star_payments
                WHERE username=? AND delivery_pending=1 AND status='paid'
                ORDER BY paid_at ASC
                """,
                (normalized,),
            )
            return cur.fetchall()

    rows = _run_with_schema_retry(_operation)
    return [_normalise_star_payment_row(_row_to_dict(row)) for row in rows if row is not None]


def mark_star_payment_pending(payment_id: int, *, error: str | None = None) -> dict | None:
    return update_star_payment_status(
        payment_id,
        delivery_pending=True,
        delivery_error=error,
        fulfilled_at=None,
    )


def mark_star_payment_fulfilled(payment_id: int) -> dict | None:
    now = _utcnow().replace(microsecond=0)
    return update_star_payment_status(
        payment_id,
        delivery_pending=False,
        delivery_error=None,
        fulfilled_at=now,
    )


def star_payments_summary(days: int | None = None) -> dict:
    cutoff_iso: str | None = None
    if days is not None and days > 0:
        cutoff_iso = (_utcnow() - timedelta(days=int(days))).replace(microsecond=0).isoformat()

    def _operation() -> list[sqlite3.Row]:
        with connect() as con:
            if cutoff_iso:
                cur = con.execute(
                    """
                    SELECT status, COUNT(*) AS cnt, COALESCE(SUM(amount_stars), 0) AS total
                    FROM star_payments
                    WHERE paid_at >= ?
                    GROUP BY status
                    """,
                    (cutoff_iso,),
                )
            else:
                cur = con.execute(
                    """
                    SELECT status, COUNT(*) AS cnt, COALESCE(SUM(amount_stars), 0) AS total
                    FROM star_payments
                    GROUP BY status
                    """
                )
            return cur.fetchall()

    rows = _run_with_schema_retry(_operation)
    summary = {"paid": {"count": 0, "total": 0}, "refunded": {"count": 0, "total": 0}, "canceled": {"count": 0, "total": 0}}
    for row in rows:
        status = (row["status"] or "").strip()
        bucket = summary.setdefault(status, {"count": 0, "total": 0})
        bucket["count"] = int(row["cnt"])
        bucket["total"] = int(row["total"])
    summary.setdefault("failed", {"count": 0, "total": 0})
    return summary

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
            expires_dt = _ensure_utc(datetime.fromisoformat(expires_raw))
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


def list_expired_keys() -> list[dict]:
    """Return active VPN keys whose expiry date is in the past."""

    cutoff = _utcnow().isoformat()

    def _operation() -> list[sqlite3.Row]:
        with connect() as con:
            cur = con.execute(
                """
                SELECT username, chat_id, uuid, link, expires_at
                FROM vpn_keys
                WHERE active=1 AND expires_at < ?
                ORDER BY expires_at ASC
                """,
                (cutoff,),
            )
            return cur.fetchall()

    rows = _run_with_schema_retry(_operation)
    expired: list[dict] = []
    for row in rows:
        expires_raw = row["expires_at"]
        try:
            expires_dt = _ensure_utc(datetime.fromisoformat(expires_raw))
        except Exception:  # pragma: no cover - defensive
            logger.warning(
                "Failed to parse expiry for expired key",
                extra={"username": row["username"], "expires_at": expires_raw},
            )
            expires_iso = expires_raw
        else:
            expires_iso = expires_dt.replace(microsecond=0).isoformat()

        expired.append(
            {
                "username": row["username"],
                "chat_id": row["chat_id"],
                "uuid": row["uuid"],
                "link": row["link"],
                "expires_at": expires_iso,
            }
        )

    if expired:
        logger.info(
            "Found expired VPN keys",
            extra={"count": len(expired)},
        )

    return expired


def schedule_renewal_notification(
    key_uuid: str,
    *,
    chat_id: int | None,
    username: str | None,
    expires_at: str | None,
) -> bool:
    if not key_uuid:
        raise ValueError("key_uuid is required")
    if chat_id is None:
        logger.info(
            "Skipping renewal notification scheduling due to missing chat_id",
            extra={"uuid": key_uuid},
        )
        return False

    now = _utcnow().isoformat()
    normalized_username = normalise_username(username) if username else None

    def _operation() -> int:
        with connect() as con:
            cursor = con.execute(
                """
                INSERT OR IGNORE INTO renewal_notifications (
                    key_uuid,
                    username,
                    chat_id,
                    expires_at,
                    stage,
                    last_sent_at,
                    next_attempt_at,
                    completed,
                    last_error,
                    created_at,
                    updated_at
                )
                VALUES (?, ?, ?, ?, 0, NULL, ?, 0, NULL, ?, ?)
                """,
                (
                    key_uuid,
                    normalized_username,
                    chat_id,
                    expires_at,
                    now,
                    now,
                    now,
                ),
            )
            return cursor.rowcount

    inserted = _run_with_schema_retry(_operation)
    if inserted:
        logger.info(
            "Scheduled renewal notification chain",
            extra={"uuid": key_uuid, "chat_id": chat_id},
        )
    else:
        logger.debug(
            "Renewal notification chain already exists",
            extra={"uuid": key_uuid, "chat_id": chat_id},
        )
    return bool(inserted)


def list_due_renewal_notifications(*, limit: int | None = None) -> list[dict]:
    cutoff = _utcnow().isoformat()

    def _operation() -> Sequence[sqlite3.Row]:
        with connect() as con:
            query = """
                SELECT id, key_uuid, username, chat_id, expires_at, stage,
                       last_sent_at, next_attempt_at, completed, last_error
                FROM renewal_notifications
                WHERE completed=0 AND next_attempt_at <= ?
                ORDER BY next_attempt_at ASC
            """
            params: tuple[Any, ...]
            if limit is not None and limit > 0:
                query += " LIMIT ?"
                params = (cutoff, int(limit))
            else:
                params = (cutoff,)
            cur = con.execute(query, params)
            return cur.fetchall()

    rows = _run_with_schema_retry(_operation)
    return [_row_to_dict(row) for row in rows]


def mark_notification_sent(
    notification_id: int,
    *,
    has_more: bool,
    interval_hours: float = _DEFAULT_NOTIFICATION_INTERVAL_HOURS,
) -> None:
    now = _utcnow()
    next_attempt = now + timedelta(hours=interval_hours) if has_more else now
    completed = 0 if has_more else 1

    def _operation() -> None:
        with connect() as con:
            con.execute(
                """
                UPDATE renewal_notifications
                SET stage = stage + 1,
                    last_sent_at = ?,
                    next_attempt_at = ?,
                    completed = ?,
                    updated_at = ?,
                    last_error = NULL
                WHERE id=?
                """,
                (
                    now.isoformat(),
                    next_attempt.isoformat(),
                    completed,
                    now.isoformat(),
                    notification_id,
                ),
            )

    _run_with_schema_retry(_operation)


def mark_notification_failed(
    notification_id: int,
    error: str,
    *,
    retry_hours: float = _DEFAULT_NOTIFICATION_RETRY_HOURS,
) -> None:
    now = _utcnow()
    next_attempt = now + timedelta(hours=retry_hours)
    message = (error or "").strip()
    if len(message) > 500:
        message = message[:500]

    def _operation() -> None:
        with connect() as con:
            con.execute(
                """
                UPDATE renewal_notifications
                SET last_error = ?,
                    next_attempt_at = ?,
                    updated_at = ?
                WHERE id=?
                """,
                (
                    message,
                    next_attempt.isoformat(),
                    now.isoformat(),
                    notification_id,
                ),
            )

    _run_with_schema_retry(_operation)


def complete_renewal_notification(notification_id: int) -> None:
    now = _utcnow()

    def _operation() -> None:
        with connect() as con:
            con.execute(
                """
                UPDATE renewal_notifications
                SET completed = 1,
                    stage = CASE
                        WHEN stage < ? THEN ?
                        ELSE stage
                    END,
                    next_attempt_at = ?,
                    updated_at = ?,
                    last_error = NULL
                WHERE id=?
                """,
                (
                    RENEWAL_NOTIFICATION_STAGE_COUNT,
                    RENEWAL_NOTIFICATION_STAGE_COUNT,
                    now.isoformat(),
                    now.isoformat(),
                    notification_id,
                ),
            )

    _run_with_schema_retry(_operation)


def get_renewal_notification(key_uuid: str) -> dict | None:
    if not key_uuid:
        raise ValueError("key_uuid is required")

    def _operation() -> sqlite3.Row | None:
        with connect() as con:
            cur = con.execute(
                """
                SELECT id, key_uuid, username, chat_id, expires_at, stage,
                       last_sent_at, next_attempt_at, completed, last_error
                FROM renewal_notifications
                WHERE key_uuid=?
                """,
                (key_uuid,),
            )
            return cur.fetchone()

    row = _run_with_schema_retry(_operation)
    return _row_to_dict(row)


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


def extend_active_key(
    username: str, *, days: int, is_subscription: bool | None = None
) -> dict | None:
    username = normalise_username(username)
    key = get_active_key(username)
    now = _utcnow()
    if key:
        current_expiry = _ensure_utc(datetime.fromisoformat(key["expires_at"]))
        if current_expiry < now:
            current_expiry = now
        new_expiry = current_expiry + timedelta(days=days)
        update_key_expiry(key["uuid"], new_expiry, is_subscription=is_subscription)
        key["expires_at"] = new_expiry.replace(microsecond=0).isoformat()
        if is_subscription is not None:
            key["is_subscription"] = bool(is_subscription)
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

            if "is_subscription" not in columns:
                logger.warning(
                    "Adding missing 'is_subscription' column to vpn_keys table",
                    extra={"path": str(resolved)},
                )
                con.execute(
                    "ALTER TABLE vpn_keys ADD COLUMN is_subscription INTEGER NOT NULL DEFAULT 0"
                )
                logger.info(
                    "Successfully added 'is_subscription' column to vpn_keys table",
                    extra={"path": str(resolved)},
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
                from api.db.migrations.migration_202409210001_add_payments_order_id import (
                    ensure_order_id_column,
                )

                ensure_order_id_column(con)
                payment_columns = _table_columns(con, "payments")
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

            star_columns = _table_columns(con, "star_payments")
            if not star_columns:
                logger.warning(
                    "Creating missing 'star_payments' table", extra={"path": str(resolved)}
                )
                con.executescript(
                    """
                    CREATE TABLE star_payments (
                      id INTEGER PRIMARY KEY AUTOINCREMENT,
                      user_id INTEGER NOT NULL,
                      username TEXT,
                      plan TEXT NOT NULL,
                      amount_stars INTEGER NOT NULL,
                      charge_id TEXT,
                      is_subscription INTEGER NOT NULL DEFAULT 0,
                      status TEXT NOT NULL,
                      paid_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                      refunded_at TIMESTAMP,
                      fulfilled_at TIMESTAMP,
                      delivery_pending INTEGER NOT NULL DEFAULT 0,
                      delivery_attempts INTEGER NOT NULL DEFAULT 0,
                      last_delivery_attempt TIMESTAMP,
                      delivery_error TEXT
                    );
                    CREATE INDEX IF NOT EXISTS idx_star_payments_user ON star_payments(user_id);
                    CREATE INDEX IF NOT EXISTS idx_star_payments_charge ON star_payments(charge_id);
                    CREATE INDEX IF NOT EXISTS idx_star_payments_status ON star_payments(status);
                    """
                )
            else:
                if "fulfilled_at" not in star_columns:
                    logger.warning(
                        "Adding missing 'fulfilled_at' column to star_payments", extra={"path": str(resolved)}
                    )
                    con.execute("ALTER TABLE star_payments ADD COLUMN fulfilled_at TIMESTAMP")
                if "delivery_pending" not in star_columns:
                    logger.warning(
                        "Adding missing 'delivery_pending' column to star_payments", extra={"path": str(resolved)}
                    )
                    con.execute(
                        "ALTER TABLE star_payments ADD COLUMN delivery_pending INTEGER NOT NULL DEFAULT 0"
                    )
                if "delivery_attempts" not in star_columns:
                    logger.warning(
                        "Adding missing 'delivery_attempts' column to star_payments", extra={"path": str(resolved)}
                    )
                    con.execute(
                        "ALTER TABLE star_payments ADD COLUMN delivery_attempts INTEGER NOT NULL DEFAULT 0"
                    )
                if "last_delivery_attempt" not in star_columns:
                    logger.warning(
                        "Adding missing 'last_delivery_attempt' column to star_payments", extra={"path": str(resolved)}
                    )
                    con.execute("ALTER TABLE star_payments ADD COLUMN last_delivery_attempt TIMESTAMP")
                if "delivery_error" not in star_columns:
                    logger.warning(
                        "Adding missing 'delivery_error' column to star_payments", extra={"path": str(resolved)}
                    )
                    con.execute("ALTER TABLE star_payments ADD COLUMN delivery_error TEXT")
                _apply_indexes(con)

            if not _table_exists(con, "renewal_notifications"):
                logger.warning(
                    "Creating missing 'renewal_notifications' table", extra={"path": str(resolved)}
                )
                con.execute(
                    """
                    CREATE TABLE renewal_notifications (
                      id INTEGER PRIMARY KEY AUTOINCREMENT,
                      key_uuid TEXT NOT NULL UNIQUE,
                      username TEXT,
                      chat_id INTEGER,
                      expires_at TEXT,
                      stage INTEGER NOT NULL DEFAULT 0,
                      last_sent_at TEXT,
                      next_attempt_at TEXT NOT NULL,
                      completed INTEGER NOT NULL DEFAULT 0,
                      last_error TEXT,
                      created_at TEXT NOT NULL,
                      updated_at TEXT NOT NULL
                    )
                    """
                )
                con.execute(
                    "CREATE INDEX IF NOT EXISTS idx_renewal_notifications_next_attempt ON renewal_notifications(next_attempt_at)"
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
    "list_broadcast_targets",
    "get_users_summary",
    "list_user_keys",
    "create_vpn_key",
    "update_key_expiry",
    "deactivate_key",
    "get_payment_by_order",
    "create_payment",
    "get_payment",
    "update_payment_status",
    "create_star_payment",
    "get_star_payment",
    "get_star_payment_by_charge",
    "update_star_payment_status",
    "list_pending_star_payments",
    "mark_star_payment_pending",
    "mark_star_payment_fulfilled",
    "star_payments_summary",
    "log_referral_bonus",
    "list_expiring_keys",
    "list_expired_keys",
    "schedule_renewal_notification",
    "list_due_renewal_notifications",
    "mark_notification_sent",
    "mark_notification_failed",
    "complete_renewal_notification",
    "get_renewal_notification",
    "referral_bonus_exists",
    "get_referral_stats",
    "extend_active_key",
    "auto_update_missing_fields",
]
