from __future__ import annotations

import os
import sqlite3

import requests
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from api.utils.auth import require_admin
from api.utils import db
from api.utils.logging import get_logger

router = APIRouter(prefix="/notify", tags=["notify"])

logger = get_logger("endpoints.notify")


def _get_bot_token() -> str | None:
    token = os.getenv("BOT_TOKEN")
    if not token:
        logger.error("BOT_TOKEN is not configured")
    return token


def _ensure_tg_users_table(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS tg_users (
            username TEXT,
            chat_id  INTEGER
        )
        """
    )


@router.post("/send")
async def send_message(request: Request, _: None = Depends(require_admin)):
    """Отправить личное уведомление конкретному пользователю."""

    data = await request.json()
    username = (data.get("username") or "").strip()
    text = (data.get("text") or "").strip()

    if not username or not text:
        logger.warning("Notification request missing fields")
        return JSONResponse(
            status_code=400,
            content={
                "ok": False,
                "error": "missing_fields",
                "message": "Необходимо указать username и текст сообщения.",
            },
        )

    token = _get_bot_token()
    if not token:
        return JSONResponse(
            status_code=500,
            content={
                "ok": False,
                "error": "bot_token_not_configured",
                "message": "Не задан BOT_TOKEN для Telegram.",
            },
        )

    with db.connect() as connection:
        _ensure_tg_users_table(connection)
        cur = connection.execute(
            "SELECT chat_id FROM tg_users WHERE username=?", (username,)
        )
        row = cur.fetchone()

    if not row or row["chat_id"] is None:
        logger.error("Chat ID not found for username", extra={"username": username})
        return JSONResponse(
            status_code=404,
            content={
                "ok": False,
                "error": "chat_id_not_found",
                "message": "Не удалось найти chat_id пользователя.",
            },
        )

    chat_id = row["chat_id"]

    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text},
            timeout=10,
        )
    except requests.RequestException as exc:
        logger.exception(
            "Telegram API request failed", extra={"username": username, "error": str(exc)}
        )
        return JSONResponse(
            status_code=500,
            content={
                "ok": False,
                "error": "telegram_request_failed",
                "message": "Не удалось отправить сообщение в Telegram.",
            },
        )

    if resp.status_code != 200:
        logger.error(
            "Telegram API returned error",
            extra={
                "username": username,
                "status_code": resp.status_code,
                "response": resp.text,
            },
        )
        return JSONResponse(
            status_code=409,
            content={
                "ok": False,
                "error": "telegram_error",
                "message": "Telegram вернул ошибку при отправке сообщения.",
            },
        )

    logger.info(
        "Notification delivered", extra={"username": username, "chat_id": chat_id}
    )
    return {"ok": True, "message": "Уведомление отправлено."}


@router.post("/broadcast")
async def notify_broadcast(request: Request, _: None = Depends(require_admin)):
    """Массовая рассылка сообщений всем пользователям из tg_users."""

    data = await request.json()
    text = (data.get("text") or "").strip()
    if not text:
        logger.warning("Broadcast request missing text")
        return JSONResponse(
            status_code=400,
            content={
                "ok": False,
                "error": "missing_text",
                "message": "Текст рассылки не может быть пустым.",
            },
        )

    token = _get_bot_token()
    if not token:
        return JSONResponse(
            status_code=500,
            content={
                "ok": False,
                "error": "bot_token_not_configured",
                "message": "Не задан BOT_TOKEN для Telegram.",
            },
        )

    with db.connect() as connection:
        _ensure_tg_users_table(connection)
        cur = connection.execute("SELECT chat_id FROM tg_users WHERE chat_id IS NOT NULL")
        rows = cur.fetchall()

    sent = 0
    for row in rows:
        chat_id = row["chat_id"]
        try:
            resp = requests.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat_id, "text": text},
                timeout=10,
            )
        except requests.RequestException as exc:
            logger.exception(
                "Broadcast Telegram API request failed",
                extra={"chat_id": chat_id, "error": str(exc)},
            )
            continue

        if resp.status_code == 200:
            sent += 1
        else:
            logger.error(
                "Failed to send broadcast message",
                extra={
                    "chat_id": chat_id,
                    "status_code": resp.status_code,
                    "response": resp.text,
                },
            )

    logger.info("Broadcast complete", extra={"sent": sent, "total": len(rows)})
    return {
        "ok": True,
        "message": f"Рассылка завершена. Отправлено {sent} из {len(rows)} сообщений.",
    }
