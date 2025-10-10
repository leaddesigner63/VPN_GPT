"""User management endpoints with detailed logging."""
from __future__ import annotations

import os
import sqlite3
from typing import Any

from fastapi import APIRouter, Header, HTTPException

from api.utils import db
from api.utils.logging import get_logger

router = APIRouter()
logger = get_logger("endpoints.users")

ADMIN_TOKEN = os.getenv("ADMIN_TOKEN")


def require_admin(x_admin_token: str | None) -> None:
    """Ensure the caller provided a valid admin token."""

    if not ADMIN_TOKEN:
        logger.error("ADMIN_TOKEN is not configured; denying access")
        raise HTTPException(status_code=500, detail="Admin token is not configured")

    if not x_admin_token or x_admin_token != ADMIN_TOKEN:
        logger.warning("Admin authentication failed")
        raise HTTPException(status_code=401, detail="Unauthorized")

    logger.debug("Admin authentication successful")


@router.get("")
def list_users(active_only: bool = True, x_admin_token: str | None = Header(default=None)) -> dict[str, Any]:
    """Return VPN users from the shared SQLite database."""

    require_admin(x_admin_token)
    users = db.get_users(active_only=active_only)
    logger.info("Returning %d users", len(users))
    return {"ok": True, "users": users}


@router.get("/expiring")
def expiring(days: int = 1, x_admin_token: str | None = Header(default=None)) -> dict[str, Any]:
    """Return users whose subscription expires within the given number of days."""

    require_admin(x_admin_token)
    users = db.get_expiring_users(days=days)
    logger.info("Returning %d expiring users", len(users))
    return {"ok": True, "users": users}


@router.get("/all")
async def list_all_users() -> dict[str, Any]:
    """Return all Telegram users stored in the local table."""

    logger.debug("Fetching Telegram users from tg_users table")
    conn = sqlite3.connect("/root/VPN_GPT/dialogs.db")
    try:
        cur = conn.cursor()
        cur.execute("CREATE TABLE IF NOT EXISTS tg_users (username TEXT, chat_id INTEGER)")
        cur.execute("SELECT username, chat_id FROM tg_users")
        rows = cur.fetchall()
    finally:
        conn.close()

    users = [{"username": u, "chat_id": c} for u, c in rows]
    logger.info("Returning %d Telegram users", len(users))
    return {"ok": True, "users": users}
