"""Notification helpers for Telegram messaging."""
from __future__ import annotations

import os

from fastapi import APIRouter, Header, HTTPException, status
from pydantic import BaseModel, Field

from api.utils import db
from api.utils.telegram import broadcast, send_message

router = APIRouter()
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN")


def require_admin(x_admin_token: str | None) -> None:
    if not ADMIN_TOKEN or x_admin_token != ADMIN_TOKEN:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Unauthorized")


class OneMessage(BaseModel):
    chat_id: int
    text: str = Field(..., min_length=1)


@router.post("/send")
async def send_one(payload: OneMessage, x_admin_token: str | None = Header(default=None)):
    require_admin(x_admin_token)
    res = await send_message(payload.chat_id, payload.text)
    return {"ok": True, "result": res}


class BroadcastIn(BaseModel):
    text: str = Field(..., min_length=1)
    chat_ids: list[int] | None = None  # если не задано — всем активным


@router.post("/broadcast")
async def do_broadcast(payload: BroadcastIn, x_admin_token: str | None = Header(default=None)):
    require_admin(x_admin_token)
    if payload.chat_ids is None:
        users = db.get_users(active_only=True)
        chat_ids = [int(u["user_id"]) for u in users if u.get("user_id")]
    else:
        chat_ids = payload.chat_ids
    res = await broadcast(chat_ids, payload.text)
    return {"ok": True, "count": len(chat_ids), "results": res}
