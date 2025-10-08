from __future__ import annotations




from api.utils.vless import build_vless_link

import os

from fastapi import APIRouter, Header, HTTPException

from api.utils import db

router = APIRouter()
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN")


def require_admin(x_admin_token: str | None):
    if not x_admin_token or x_admin_token != ADMIN_TOKEN:
        raise HTTPException(status_code=401, detail="Unauthorized")


@router.get("")
def list_users(active_only: bool = True, x_admin_token: str | None = Header(default=None)):
    require_admin(x_admin_token)
    return {"ok": True, "users": db.get_users(active_only=active_only)}


@router.get("/expiring")
def expiring(days: int = 1, x_admin_token: str | None = Header(default=None)):
    require_admin(x_admin_token)
    return {"ok": True, "users": db.get_expiring_users(days=days)}
