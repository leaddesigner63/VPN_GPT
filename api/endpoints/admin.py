"""Administrative endpoints for the VPN_GPT project."""

from __future__ import annotations

import os
import secrets
import shutil
from datetime import UTC, datetime
from pathlib import Path

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel

from api import config
from api.utils import db
from api.utils.logging import get_logger

router = APIRouter()
DB_PATH = Path(db.DB_PATH)
logger = get_logger("endpoints.admin")


def require_admin(x_admin_token: str | None) -> None:
    """Ensure that the provided admin token is valid."""

    if not x_admin_token or x_admin_token != config.ADMIN_TOKEN:
        logger.warning("Unauthorized admin request")
        raise HTTPException(status_code=401, detail="Unauthorized")
    logger.debug("Authorized admin request")


class AdminAuthPayload(BaseModel):
    """Request payload for admin password authentication."""

    password: str


class AdminAuthResponse(BaseModel):
    """Response returned after successful authentication."""

    ok: bool
    admin_token: str | None = None


@router.post("/auth", response_model=AdminAuthResponse, tags=["admin"], include_in_schema=False)
def authenticate_admin(payload: AdminAuthPayload) -> AdminAuthResponse:
    """Validate the admin password and return the API token."""

    password = payload.password.strip()
    if not password:
        logger.warning("Admin authentication attempt with empty password")
        raise HTTPException(status_code=400, detail="Пароль обязателен")

    if not secrets.compare_digest(password, config.ADMIN_PANEL_PASSWORD):
        logger.warning("Invalid admin password provided")
        raise HTTPException(status_code=401, detail="Неверный пароль")

    logger.info("Admin authentication successful")
    return AdminAuthResponse(ok=True, admin_token=config.ADMIN_TOKEN)


@router.post("/backup_db")
def backup_db(
    x_admin_token: str | None = Header(
        default=None,
        alias="X-Admin-Token",
        include_in_schema=False,
    )
) -> dict[str, str | bool]:
    """Create a timestamped database backup."""

    require_admin(x_admin_token)
    ts = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    backup_root = os.getenv("BACKUP_DIR")
    backup_dir = Path(backup_root) if backup_root else DB_PATH.parent
    backup_dir.mkdir(parents=True, exist_ok=True)
    dest = backup_dir / f"db-backup-{ts}.sqlite3"
    shutil.copyfile(DB_PATH, dest)
    logger.info("Created database backup", extra={"destination": str(dest)})
    return {"ok": True, "backup": str(dest)}


__all__ = ["router"]
