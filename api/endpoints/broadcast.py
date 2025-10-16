import os
from typing import Any

import requests
from fastapi import APIRouter
from pydantic import BaseModel

from api.utils import db
from api.utils.logging import get_logger
from utils.content_filters import assert_no_geoblocking, sanitize_text

router = APIRouter()
BOT_TOKEN = os.getenv("BOT_TOKEN")
logger = get_logger("endpoints.broadcast")

class BroadcastRequest(BaseModel):
    text: str


@router.post("/notify/broadcast")
async def notify_broadcast(payload: BroadcastRequest) -> dict[str, Any]:
    """Массовая рассылка сообщений всем пользователям из tg_users."""
    text = (payload.text or "").strip()
    if not text:
        logger.warning("Broadcast request missing text")
        return {"ok": False, "error": "missing_text"}

    if not BOT_TOKEN:
        logger.error("Broadcast request rejected: BOT_TOKEN is not configured")
        return {"ok": False, "error": "bot_token_not_configured"}

    targets = db.list_broadcast_targets()
    if not targets:
        logger.info("No Telegram users registered for broadcast")
        return {"ok": True, "sent": 0, "total": 0}

    safe_text = sanitize_text(text)
    assert_no_geoblocking(safe_text)

    logger.info("Starting broadcast to Telegram users", extra={"count": len(targets)})
    sent = 0
    for target in targets:
        chat_id = target["chat_id"]
        resp = requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json={"chat_id": chat_id, "text": safe_text},
            timeout=10,
        )
        if resp.status_code == 200:
            sent += 1
        else:
            logger.error(
                "Failed to send broadcast message",
                extra={
                    "chat_id": chat_id,
                    "username": target.get("username"),
                    "status_code": resp.status_code,
                    "response": resp.text,
                },
            )

    logger.info("Broadcast complete", extra={"sent": sent, "total": len(targets)})
    return {"ok": True, "sent": sent, "total": len(targets)}
