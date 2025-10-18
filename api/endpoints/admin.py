"""Administrative endpoints for the VPN_GPT project."""

from __future__ import annotations

import os
import secrets
import shutil
from datetime import UTC, datetime
from pathlib import Path

from fastapi import APIRouter, Header, HTTPException, Request, Response
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


class AdminAuthResponse(BaseModel):
    """Response returned after successful authentication."""

    ok: bool
    admin_token: str | None = None


@router.post(
    "/auth",
    response_model=AdminAuthResponse,
    tags=["admin"],
    include_in_schema=False,
)
async def authenticate_admin(request: Request) -> AdminAuthResponse:
    """Validate the admin password and return the API token."""

    raw_password: str | None = None
    content_type = request.headers.get("content-type", "").lower()
    try:
        if content_type.startswith("application/json"):
            payload = await request.json()
            if isinstance(payload, dict):
                raw_password = payload.get("password")
        else:
            form = await request.form()
            raw_password = form.get("password")
    except Exception as exc:  # pragma: no cover - defensive logging
        logger.warning("Failed to parse admin auth payload", exc_info=exc)

    password = (raw_password or "").strip()
    if not password:
        logger.warning("Admin authentication attempt with empty password")
        raise HTTPException(status_code=400, detail="Пароль обязателен")

    if not secrets.compare_digest(password, config.ADMIN_PANEL_PASSWORD):
        logger.warning("Invalid admin password provided")
        raise HTTPException(status_code=401, detail="Неверный пароль")

    logger.info("Admin authentication successful")
    return AdminAuthResponse(ok=True, admin_token=config.ADMIN_TOKEN)


@router.options("/auth", include_in_schema=False)
def admin_auth_preflight() -> Response:
    """Handle CORS pre-flight requests for the password auth endpoint."""

    response = Response(status_code=204)
    response.headers["Allow"] = "POST, OPTIONS"
    response.headers["Access-Control-Allow-Methods"] = "POST, OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type, Accept"
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Max-Age"] = "600"
    return response


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
