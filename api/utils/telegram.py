from __future__ import annotations




from api.utils.vless import build_vless_link
import os
import httpx

BOT_TOKEN = os.getenv("BOT_TOKEN")
BASE = os.getenv("BASE_BOT_URL", "https://api.telegram.org")

def _url(method: str) -> str:
    return f"{BASE}/bot{BOT_TOKEN}/{method}"

async def send_message(chat_id: int | str, text: str, parse_mode: str | None = None):
    async with httpx.AsyncClient(timeout=30) as client:
        data = {"chat_id": chat_id, "text": text}
        if parse_mode:
            data["parse_mode"] = parse_mode
        r = await client.post(_url("sendMessage"), json=data)
        r.raise_for_status()
        return r.json()

async def broadcast(chat_ids: list[int | str], text: str):
    results = []
    for cid in chat_ids:
        try:
            results.append(await send_message(cid, text))
        except Exception as e:
            results.append({"chat_id": cid, "ok": False, "error": str(e)})
    return results

