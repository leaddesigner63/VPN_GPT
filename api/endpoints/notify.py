import os
import sqlite3

import requests
from fastapi import APIRouter, Request

from api.utils.logging import get_logger

router = APIRouter()

BOT_TOKEN = os.getenv("BOT_TOKEN")
logger = get_logger("endpoints.notify")

@router.post("/notify/send")
async def send_message(request: Request):
    """Отправка сообщения пользователю по username через сохранённый chat_id."""
    data = await request.json()
    username = data.get("username")
    text = data.get("text")

    if not username or not text:
        logger.warning("Notification request missing fields")
        return {"ok": False, "error": "missing_fields"}

    logger.info("Sending notification", extra={"username": username})
    conn = sqlite3.connect("/root/VPN_GPT/dialogs.db")
    cur = conn.cursor()
    cur.execute("SELECT chat_id FROM tg_users WHERE username=?", (username,))
    row = cur.fetchone()
    conn.close()

    if not row:
        logger.error("Chat ID not found for username", extra={"username": username})
        return {"ok": False, "error": "chat_id_not_found"}

    chat_id = row[0]
    resp = requests.post(
        f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
        json={"chat_id": chat_id, "text": text}
    )

    if resp.status_code == 200:
        logger.info("Notification delivered", extra={"username": username, "chat_id": chat_id})
        return {"ok": True, "username": username, "chat_id": chat_id}
    else:
        logger.error(
            "Telegram API returned error",
            extra={"username": username, "status_code": resp.status_code, "response": resp.text},
        )
        return {"ok": False, "error": f"telegram_error {resp.status_code}", "details": resp.text}
