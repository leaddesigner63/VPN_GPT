from __future__ import annotations




import os

import httpx

from api.utils.logging import get_logger

BOT_TOKEN = os.getenv("BOT_TOKEN")
BASE = os.getenv("BASE_BOT_URL", "https://api.telegram.org")
logger = get_logger("utils.telegram")

def _url(method: str) -> str:
    return f"{BASE}/bot{BOT_TOKEN}/{method}"

async def send_message(chat_id: int | str, text: str, parse_mode: str | None = None):
    logger.info("Sending Telegram message", extra={"chat_id": chat_id})
    async with httpx.AsyncClient(timeout=30) as client:
        data = {"chat_id": chat_id, "text": text}
        if parse_mode:
            data["parse_mode"] = parse_mode
        r = await client.post(_url("sendMessage"), json=data)
        r.raise_for_status()
        logger.debug("Telegram API response", extra={"chat_id": chat_id, "status_code": r.status_code})
        return r.json()

async def broadcast(chat_ids: list[int | str], text: str):
    results = []
    for cid in chat_ids:
        try:
            results.append(await send_message(cid, text))
        except Exception as e:
            logger.exception("Failed to send Telegram message", extra={"chat_id": cid})
            results.append({"chat_id": cid, "ok": False, "error": str(e)})
        else:
            logger.info("Telegram message sent successfully", extra={"chat_id": cid})
    return results

