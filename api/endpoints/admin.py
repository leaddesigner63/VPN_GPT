from __future__ import annotations

import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from api.utils import db
from api.utils.auth import require_admin
from api.utils.logging import get_logger

router = APIRouter()
DB_PATH = Path(db.DB_PATH)
logger = get_logger("endpoints.admin")


def _validate_limit(limit: int | None) -> int | None:
    if limit is None:
        return None
    if limit < 1 or limit > 500:
        raise ValueError
    return limit


def _stats_not_found(message: str) -> JSONResponse:
    return JSONResponse(
        status_code=404,
        content={"ok": False, "error": "stats_not_available", "message": message},
    )


@router.post("/backup_db")
def backup_db(_: None = Depends(require_admin)) -> dict[str, Any]:
    ts = datetime.utcnow().strftime("%Y%m%d-%H%M%S")
    backup_dir = Path(os.getenv("BACKUP_DIR", DB_PATH.parent))
    backup_dir.mkdir(parents=True, exist_ok=True)
    dest = backup_dir / f"db-backup-{ts}.sqlite3"
    shutil.copyfile(DB_PATH, dest)

    backup_id = dest.stem
    created_at = datetime.now(timezone.utc).isoformat()
    location = dest.resolve().as_uri()

    logger.info("Created database backup", extra={"destination": str(dest)})
    return {
        "ok": True,
        "backup_id": backup_id,
        "created_at": created_at,
        "location": location,
        "message": "Резервная копия создана.",
    }


def _is_key_expired(row: dict[str, Any]) -> bool:
    expires_at = row.get("expires_at")
    if not expires_at:
        return not bool(row.get("active"))

    try:
        expiry = datetime.strptime(str(expires_at), "%Y-%m-%d").date()
    except ValueError:
        logger.debug("Invalid expiry format", extra={"expires_at": expires_at})
        return not bool(row.get("active"))

    today = datetime.utcnow().date()
    if expiry < today:
        return True

    return not bool(row.get("active"))


@router.get("/stats")
def get_stats(
    username: str | None = None,
    active: bool | None = None,
    limit: int | None = None,
    _: None = Depends(require_admin),
) -> dict[str, Any] | JSONResponse:
    try:
        limit = _validate_limit(limit)
    except ValueError:
        return JSONResponse(
            status_code=409,
            content={
                "ok": False,
                "error": "invalid_limit",
                "message": "Параметр limit должен быть в диапазоне 1..500.",
            },
        )

    users = db.get_users(username=username, active=active, limit=limit)
    if not users:
        return _stats_not_found("Статистика недоступна для выбранных параметров.")

    active_keys = 0
    expired_keys = 0

    for row in users:
        expired = _is_key_expired(row)
        if row.get("active") and not expired:
            active_keys += 1
        if expired:
            expired_keys += 1

    result = {
        "ok": True,
        "active_keys": active_keys,
        "expired_keys": expired_keys,
        "users_total": len(users),
    }

    logger.info(
        "Returning admin stats",
        extra={"active_keys": active_keys, "expired_keys": expired_keys, "total": len(users)},
    )
    return result

