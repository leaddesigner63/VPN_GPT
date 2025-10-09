from fastapi import APIRouter, Request
import sqlite3, os, requests

router = APIRouter()
BOT_TOKEN = os.getenv("BOT_TOKEN")

@router.post("/notify/broadcast")
async def notify_broadcast(request: Request):
    """Массовая рассылка сообщений всем пользователям из tg_users."""
    data = await request.json()
    text = data.get("text")
    if not text:
        return {"ok": False, "error": "missing_text"}

    conn = sqlite3.connect("/root/VPN_GPT/dialogs.db")
    cur = conn.cursor()
    cur.execute("CREATE TABLE IF NOT EXISTS tg_users (username TEXT, chat_id INTEGER)")
    cur.execute("SELECT chat_id FROM tg_users")
    rows = cur.fetchall()
    conn.close()

    sent = 0
    for (chat_id,) in rows:
        resp = requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                             json={"chat_id": chat_id, "text": text})
        if resp.status_code == 200:
            sent += 1

    return {"ok": True, "sent": sent, "total": len(rows)}
