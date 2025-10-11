"""Authentication utilities for FastAPI endpoints."""

from __future__ import annotations

import os

from fastapi import Header, HTTPException

from api.utils.logging import get_logger

logger = get_logger("auth")


def _get_admin_token() -> str | None:
    """Return the configured admin token if present."""

    token = os.getenv("ADMIN_TOKEN")
    if not token:
        logger.error("ADMIN_TOKEN is not configured; refusing admin request")
    return token


def require_admin(x_admin_token: str | None = Header(default=None)) -> None:
    """Validate that the request is authorised with the admin token."""

    token = _get_admin_token()
    if not token:
        raise HTTPException(status_code=500, detail="Admin token is not configured")

    if not x_admin_token or x_admin_token != token:
        logger.warning("Admin authentication failed")
        raise HTTPException(status_code=401, detail="Unauthorized")

    logger.debug("Admin authentication successful")


__all__ = ["require_admin"]

