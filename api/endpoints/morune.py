import os
from datetime import datetime, timedelta
from fastapi import APIRouter, Request
from api.utils import db
from api.utils.xray import add_client

router = APIRouter()

MORUNE_WEBHOOK_SECRET = os.getenv("MORUNE_WEBHOOK_SECRET")

def verify_signature(request: Request) -> bool:
    # Простейший заглушечный вариант.
    sig = request.headers.get("X-Morune-Signature")
    return bool(MORUNE_WEBHOOK_SECRET) and sig == MORUNE_WEBHOOK_SECRET

@router.post("/webhook")
async def morune_webhook(request: Request):
    # ожидаем JSON: {"status":"paid","user_id":"...","username":"...","days":30}
    payload = await request.json()
    # в реальности проверь событие/статус, подпись и id платежа
    if not verify_signature(request):
        return {"ok": False, "error": "bad signature"}

    if payload.get("status") != "paid":
        return {"ok": True, "ignored": True}

    user_id = str(payload.get("user_id"))
    username = payload.get("username") or None
    days = int(payload.get("days") or 30)

    # Автовыдача ключа после оплаты
    email = f"user_{user_id}@auto"
    x = add_client(email=email)
    uuid = x["uuid"]
    issued = datetime.utcnow()
    expires = issued + timedelta(days=days)
    link = f"vless://{uuid}@your-host:2053?security=reality#VPN_GPT"

    with db.connect() as con:
        con.execute("""
        INSERT INTO vpn_keys (user_id, username, uuid, link, issued_at, expires_at, active)
        VALUES (?, ?, ?, ?, ?, ?, 1)
        """, (user_id, username, uuid, link, issued.isoformat(), expires.isoformat()))

    return {"ok": True, "uuid": uuid, "link": link, "expires_at": expires.isoformat()}

