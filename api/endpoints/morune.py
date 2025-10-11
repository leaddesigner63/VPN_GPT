from __future__ import annotations




import os
from datetime import datetime, timedelta

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel

from sqlite3 import IntegrityError

from api.utils import db, xray
from ..utils.env import get_vless_host
from api.utils.logging import get_logger

router = APIRouter()

ADMIN_TOKEN = os.getenv("ADMIN_TOKEN")
logger = get_logger("endpoints.morune")


def require_admin(x_admin_token: str | None):
    if not x_admin_token or x_admin_token != ADMIN_TOKEN:
        logger.warning("Unauthorized Morune request")
        raise HTTPException(status_code=401, detail="Unauthorized")
    logger.debug("Authorized Morune request")


class PaymentIn(BaseModel):
    user_id: str
    username: str | None = None
    days: int = 30


@router.post("/paid")
def process_payment(payload: PaymentIn, x_admin_token: str | None = Header(default=None)):
    require_admin(x_admin_token)
    logger.info(
        "Processing payment notification",
        extra={"user_id": payload.user_id, "username": payload.username, "days": payload.days},
    )
    # Проверяем, что пользователь ещё не имеет активного ключа
    with db.connect() as con:
        conditions = ["(user_id = ? AND user_id IS NOT NULL AND user_id <> '')"]
        params: list[str | None] = [payload.user_id]
        if payload.username:
            conditions.append("(username = ? AND username IS NOT NULL AND username <> '')")
            params.append(payload.username)
        query = (
            "SELECT 1 FROM vpn_keys WHERE active=1 AND ("
            + " OR ".join(conditions)
            + ") LIMIT 1"
        )
        cur = con.execute(query, params)
        if cur.fetchone():
            logger.warning(
                "Duplicate VPN key prevented for payment",
                extra={"user_id": payload.user_id, "username": payload.username},
            )
            raise HTTPException(status_code=409, detail="user_already_has_key")

    email = f"user_{payload.user_id}@auto"
    try:
        new_client = xray.add_client(email=email)
        uuid = new_client["uuid"]
    except ValueError as exc:
        logger.warning(
            "Duplicate client id prevented for Morune user",
            extra={"user_id": payload.user_id, "username": payload.username},
        )
        raise HTTPException(status_code=409, detail="client_already_exists") from exc
    except Exception as exc:  # noqa: BLE001 - we log and propagate
        logger.exception("Failed to add Morune client to Xray", extra={"user_id": payload.user_id})
        raise
    issued = datetime.utcnow()
    expires = issued + timedelta(days=payload.days)
    vless_host = get_vless_host()
    vless_port = os.getenv("VLESS_PORT", "2053")
    link = f"vless://{uuid}@{vless_host}:{vless_port}?encryption=none#VPN_GPT"

    try:
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
    except IntegrityError:
        logger.warning(
            "Race detected: duplicate VPN key prevented during payment",
            extra={"user_id": payload.user_id, "username": payload.username},
        )
        raise HTTPException(status_code=409, detail="user_already_has_key")
    logger.info(
        "Stored new VPN key from Morune payment",
        extra={"user_id": payload.user_id, "uuid": uuid, "expires": expires.isoformat()},
    )
    return {"ok": True, "uuid": uuid, "link": link, "expires_at": expires.isoformat()}


@router.get("/check")
def morune_check(x_admin_token: str | None = Header(default=None)):
    require_admin(x_admin_token)
    with db.connect() as con:
        cur = con.execute("SELECT COUNT(*) AS active FROM vpn_keys WHERE active=1")
        row = cur.fetchone()
    active = row["active"] if row else 0
    logger.info("Morune status check", extra={"active_keys": active})
    return {"ok": True, "active_keys": active}
