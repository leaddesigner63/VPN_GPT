import os
from typing import Any

from fastapi import APIRouter, Request

from api.utils import db
from api.utils.logging import get_logger
from utils.content_filters import assert_no_geoblocking, sanitize_text

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

    try:
        normalized = db.normalise_username(username)
    except ValueError:
        return {"ok": False, "error": "invalid_username"}

    user = db.get_user(normalized)
    chat_id = user.get("chat_id") if user else None

    if not chat_id:
        logger.error("Chat ID not found for username", extra={"username": normalized})
        return {"ok": False, "error": "chat_id_not_found"}

    try:
        import requests  # type: ignore
    except ImportError:
        logger.error("requests library is not installed")
        return {"ok": False, "error": "requests_not_available"}

    safe_text = sanitize_text(text)
    assert_no_geoblocking(safe_text)

    resp = requests.post(
        f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
        json={"chat_id": chat_id, "text": safe_text},
        timeout=10,
    )

    if resp.status_code == 200:
        logger.info("Notification delivered", extra={"username": normalized, "chat_id": chat_id})
        return {"ok": True, "username": normalized, "chat_id": chat_id}

    logger.error(
        "Telegram API returned error",
        extra={"username": normalized, "status_code": resp.status_code, "response": resp.text},
    )
    return {"ok": False, "error": f"telegram_error {resp.status_code}", "details": resp.text}
