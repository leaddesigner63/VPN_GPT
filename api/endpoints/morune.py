from __future__ import annotations




from api.utils.vless import build_vless_link

import os
from datetime import datetime, timedelta

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel

from api.utils import db, xray
from ..utils.env import get_vless_host

router = APIRouter()

ADMIN_TOKEN = os.getenv("ADMIN_TOKEN")


def require_admin(x_admin_token: str | None):
    if not x_admin_token or x_admin_token != ADMIN_TOKEN:
        raise HTTPException(status_code=401, detail="Unauthorized")


class PaymentIn(BaseModel):
    user_id: str
    username: str | None = None
    days: int = 30


@router.post("/paid")
def process_payment(payload: PaymentIn, x_admin_token: str | None = Header(default=None)):
    require_admin(x_admin_token)
    email = f"user_{payload.user_id}@auto"
    new_client = xray.add_client(email=email)
    uuid = new_client["uuid"]
    issued = datetime.utcnow()
    expires = issued + timedelta(days=payload.days)
    vless_host = get_vless_host()
    vless_port = os.getenv("VLESS_PORT", "2053")
    link = f"build_vless_link(uuid, username)@{vless_host}:{vless_port}?encryption=none#VPN_GPT"

    with db.connect() as con:
        con.execute(
            """
            INSERT INTO vpn_keys (user_id, username, uuid, link, issued_at, expires_at, active)
            VALUES (?, ?, ?, ?, ?, ?, 1)
            """,
            (
                payload.user_id,
                payload.username,
                uuid,
                link,
                issued.isoformat(),
                expires.isoformat(),
            ),
        )
    return {"ok": True, "uuid": uuid, "link": link, "expires_at": expires.isoformat()}


@router.get("/check")
def morune_check(x_admin_token: str | None = Header(default=None)):
    require_admin(x_admin_token)
    with db.connect() as con:
        cur = con.execute("SELECT COUNT(*) AS active FROM vpn_keys WHERE active=1")
        row = cur.fetchone()
    return {"ok": True, "active_keys": row["active"]}
