"""User management endpoints with detailed logging."""
from __future__ import annotations

import sqlite3
from typing import Any

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from api.utils import db
from api.utils.auth import require_admin
from api.utils.logging import get_logger

router = APIRouter(prefix="/users", tags=["users"])
logger = get_logger("endpoints.users")


def _validate_limit(limit: int | None) -> int | None:
    if limit is None:
        return None
    if limit < 1 or limit > 500:
        raise ValueError("limit_out_of_range")
    return limit


def _not_found(message: str, error: str = "not_found") -> JSONResponse:
    return JSONResponse(status_code=404, content={"ok": False, "error": error, "message": message})


def _conflict(message: str, error: str = "invalid_filters") -> JSONResponse:
    return JSONResponse(status_code=409, content={"ok": False, "error": error, "message": message})


@router.get("/")
def list_users(
    username: str | None = None,
    active: bool | None = None,
    limit: int | None = None,
    _: None = Depends(require_admin),
) -> dict[str, Any] | JSONResponse:
    """Return VPN users from the shared SQLite database."""

    try:
        limit = _validate_limit(limit)
    except ValueError:
        logger.warning("Invalid limit provided", extra={"limit": limit})
        return _conflict("Параметр limit должен быть в диапазоне 1..500.")

    users = db.get_users(username=username, active=active, limit=limit)
    if not users:
        logger.info("No users matched filters", extra={"username": username, "active": active})
        return _not_found("Пользователи не найдены.", "users_not_found")

    logger.info(
        "Returning %d users", len(users), extra={"username": username, "active": active, "limit": limit}
    )
    return {"ok": True, "users": users, "total": len(users)}


@router.get("/expiring")
def expiring(
    days: int = 3,
    username: str | None = None,
    active: bool | None = None,
    limit: int | None = None,
    _: None = Depends(require_admin),
) -> dict[str, Any] | JSONResponse:
    """Return users whose subscription expires within the given number of days."""

    if days < 0:
        logger.warning("Invalid days parameter", extra={"days": days})
        return _conflict("Параметр days должен быть неотрицательным.", "invalid_days")

    try:
        limit = _validate_limit(limit)
    except ValueError:
        logger.warning("Invalid limit provided", extra={"limit": limit})
        return _conflict("Параметр limit должен быть в диапазоне 1..500.")

    users = db.get_expiring_users(days, username=username, active=active, limit=limit)
    if not users:
        logger.info(
            "No expiring users found", extra={"days": days, "username": username, "active": active}
        )
        return _not_found("Нет пользователей с истекающими ключами.", "users_not_found")

    logger.info(
        "Returning %d expiring users",
        len(users),
        extra={"days": days, "username": username, "active": active, "limit": limit},
    )
    return {"ok": True, "users": users, "total": len(users)}


@router.get("/userinfo")
def get_userinfo(
    username: str | None = None,
    _: None = Depends(require_admin),
) -> dict[str, Any] | JSONResponse:
    """Return aggregated VPN user information."""

    if not username:
        logger.warning("Username is required for userinfo endpoint")
        return _conflict("Необходимо указать username пользователя.", "missing_username")

    logger.debug("Fetching aggregated user info", extra={"username": username})
    user = db.get_vpn_user_full(username)
    if user is None:
        logger.info("User not found", extra={"username": username})
        return _not_found("Пользователь не найден.", "user_not_found")

    logger.info("Returning aggregated user info", extra={"username": username})
    return {"ok": True, "user": user}


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
