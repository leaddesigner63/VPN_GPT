"""VPN management endpoints."""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

router = APIRouter()

DB_PATH = Path(os.getenv("DATABASE", "/root/VPN_GPT/dialogs.db"))
XRAY_CONFIG = Path(os.getenv("XRAY_CONFIG", "/usr/local/etc/xray/config.json"))
XRAY_SERVICE = os.getenv("XRAY_SERVICE", "xray")
VLESS_HOST = os.getenv("VLESS_HOST", "vpn-gpt.store")
VLESS_PORT = os.getenv("VLESS_PORT", "2053")


class IssueVPNKeyPayload(BaseModel):
    username: str = Field(..., min_length=1)
    days: int = Field(30, ge=1, le=365)


class RenewVPNKeyPayload(BaseModel):
    username: str = Field(..., min_length=1)
    days: int = Field(30, ge=1, le=365)


class DisableVPNKeyPayload(BaseModel):
    uuid: str = Field(..., min_length=1)


def _with_connection() -> sqlite3.Connection:
    if not DB_PATH.parent.exists():
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _touch_xray(modifier: Callable[[list[dict]], bool]) -> None:
    if not XRAY_CONFIG.exists():
        return

    try:
        with XRAY_CONFIG.open("r", encoding="utf-8") as f:
            cfg = json.load(f)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=500, detail=f"Invalid XRAY config: {exc}") from exc

    inbounds = cfg.get("inbounds") or []
    if not inbounds:
        return

    inbound = inbounds[0]
    settings = inbound.setdefault("settings", {})
    clients = settings.setdefault("clients", [])

    changed = modifier(clients)
    if not changed:
        return

    tmp_path = XRAY_CONFIG.with_suffix(".tmp")
    with tmp_path.open("w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)
    tmp_path.replace(XRAY_CONFIG)
    subprocess.run(["systemctl", "restart", XRAY_SERVICE], check=False)


@router.post("/issue_key")
async def issue_vpn_key(payload: IssueVPNKeyPayload) -> dict:
    issued_at = datetime.utcnow()
    expires_at = issued_at + timedelta(days=payload.days)
    key_uuid = str(uuid.uuid4())
    link = f"vless://{key_uuid}@{VLESS_HOST}:{VLESS_PORT}?encryption=none#{payload.username}"

    conn = _with_connection()
    try:
        conn.execute(
            """
            INSERT INTO vpn_keys (username, uuid, active, expires)
            VALUES (?, ?, 1, ?)
            """,
            (payload.username, key_uuid, expires_at.strftime("%Y-%m-%d")),
        )
        conn.commit()
    except sqlite3.Error as exc:
        raise HTTPException(status_code=500, detail=f"DB error: {exc}") from exc
    finally:
        conn.close()

    def _add(clients: list[dict]) -> bool:
        clients.append({"id": key_uuid, "email": payload.username})
        return True

    _touch_xray(_add)

    return {
        "ok": True,
        "link": link,
        "uuid": key_uuid,
        "expires": expires_at.strftime("%Y-%m-%d"),
    }


@router.post("/renew_key")
async def renew_vpn_key(payload: RenewVPNKeyPayload) -> dict:
    conn = _with_connection()
    try:
        cur = conn.execute(
            "SELECT expires FROM vpn_keys WHERE username=? AND active=1 LIMIT 1",
            (payload.username,),
        )
        row = cur.fetchone()
        if row is None:
            return {"ok": False, "error": "user_not_found"}
        current_expires = datetime.strptime(row["expires"], "%Y-%m-%d")
        new_expires = current_expires + timedelta(days=payload.days)
        conn.execute(
            "UPDATE vpn_keys SET expires=? WHERE username=?",
            (new_expires.strftime("%Y-%m-%d"), payload.username),
        )
        conn.commit()
    except sqlite3.Error as exc:
        raise HTTPException(status_code=500, detail=f"DB error: {exc}") from exc
    finally:
        conn.close()

    return {
        "ok": True,
        "username": payload.username,
        "expires": new_expires.strftime("%Y-%m-%d"),
    }


@router.post("/disable_key")
async def disable_vpn_key(payload: DisableVPNKeyPayload) -> dict:
    conn = _with_connection()
    try:
        cur = conn.execute(
            "SELECT COUNT(1) FROM vpn_keys WHERE uuid=? AND active=1",
            (payload.uuid,),
        )
        exists = cur.fetchone()[0]
        if not exists:
            return {"ok": False, "error": "key_not_found"}
        conn.execute("UPDATE vpn_keys SET active=0 WHERE uuid=?", (payload.uuid,))
        conn.commit()
    except sqlite3.Error as exc:
        raise HTTPException(status_code=500, detail=f"DB error: {exc}") from exc
    finally:
        conn.close()

    def _remove(clients: list[dict]) -> bool:
        before = len(clients)
        clients[:] = [c for c in clients if c.get("id") != payload.uuid]
        return len(clients) != before

    _touch_xray(_remove)

    return {"ok": True, "uuid": payload.uuid}
