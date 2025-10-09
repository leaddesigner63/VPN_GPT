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
from fastapi import APIRouter
import sqlite3

router = APIRouter()

@router.get("/users")
async def list_users():
    """Список всех пользователей и их chat_id."""
    conn = sqlite3.connect("/root/VPN_GPT/dialogs.db")
    cur = conn.cursor()
    cur.execute("CREATE TABLE IF NOT EXISTS tg_users (username TEXT, chat_id INTEGER)")
    cur.execute("SELECT username, chat_id FROM tg_users")
    users = [{"username": u, "chat_id": c} for u, c in cur.fetchall()]
    conn.close()
    return {"ok": True, "users": users}
