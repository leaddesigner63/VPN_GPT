"""Endpoints for Morune payment webhook integration."""
from __future__ import annotations

import os
from datetime import datetime, timedelta

from fastapi import APIRouter, Header, HTTPException, status
from pydantic import BaseModel, Field

from api.utils import db, xray

router = APIRouter()

ADMIN_TOKEN = os.getenv("ADMIN_TOKEN")
VLESS_HOST = os.getenv("VLESS_HOST", "YOUR_HOST")
VLESS_PORT = os.getenv("VLESS_PORT", "2053")


def require_admin(x_admin_token: str | None) -> None:
    if not ADMIN_TOKEN or x_admin_token != ADMIN_TOKEN:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Unauthorized")


class PaymentIn(BaseModel):
    user_id: str = Field(..., min_length=1)
    username: str | None = None
    days: int = Field(30, ge=1, le=365)


@router.post("/paid")
def process_payment(payload: PaymentIn, x_admin_token: str | None = Header(default=None)):
    require_admin(x_admin_token)

    try:
        new_client = xray.add_client(email=f"user_{payload.user_id}@auto")
    except FileNotFoundError as exc:
        raise HTTPException(status_code=500, detail="XRAY config not found") from exc
    except Exception as exc:  # pragma: no cover - defensive programming
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    client_uuid = new_client["uuid"]
    issued = datetime.utcnow()
    expires = issued + timedelta(days=payload.days)
    link = f"vless://{client_uuid}@{VLESS_HOST}:{VLESS_PORT}?encryption=none#VPN_GPT"

    with db.connect() as con:
        con.execute(
            """
            INSERT INTO vpn_keys (user_id, username, uuid, link, issued_at, expires_at, active)
            VALUES (?, ?, ?, ?, ?, ?, 1)
            """,
            (
                payload.user_id,
                payload.username,
                client_uuid,
                link,
                issued.isoformat(),
                expires.isoformat(),
            ),
        )

    return {
        "ok": True,
        "uuid": client_uuid,
        "link": link,
        "expires_at": expires.isoformat(),
    }


@router.get("/check")
def morune_check(x_admin_token: str | None = Header(default=None)):
    require_admin(x_admin_token)
    with db.connect() as con:
        cur = con.execute("SELECT COUNT(*) AS active FROM vpn_keys WHERE active=1")
        row = cur.fetchone()
    return {"ok": True, "active_keys": row["active"]}
