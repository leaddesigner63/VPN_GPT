import os
import sqlite3

import requests
from fastapi import APIRouter, Request

from api.utils.logging import get_logger

router = APIRouter()
BOT_TOKEN = os.getenv("BOT_TOKEN")
logger = get_logger("endpoints.broadcast")

@router.post("/notify/broadcast")
async def notify_broadcast(request: Request):
    """Массовая рассылка сообщений всем пользователям из tg_users."""
    data = await request.json()
    text = data.get("text")
    if not text:
        logger.warning("Broadcast request missing text")
        return {"ok": False, "error": "missing_text"}

    logger.info("Starting broadcast to Telegram users")
    conn = sqlite3.connect("/root/VPN_GPT/dialogs.db")
    cur = conn.cursor()
    cur.execute("CREATE TABLE IF NOT EXISTS tg_users (username TEXT, chat_id INTEGER)")
    cur.execute("SELECT chat_id FROM tg_users")
    rows = cur.fetchall()
    conn.close()

    sent = 0
    for (chat_id,) in rows:
        resp = requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json={"chat_id": chat_id, "text": text},
        )
        if resp.status_code == 200:
            sent += 1
        else:
            logger.error(
                "Failed to send broadcast message",
                extra={"chat_id": chat_id, "status_code": resp.status_code, "response": resp.text},
            )

    logger.info("Broadcast complete", extra={"sent": sent, "total": len(rows)})
    return {"ok": True, "sent": sent, "total": len(rows)}
