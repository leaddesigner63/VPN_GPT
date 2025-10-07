from __future__ import annotations

import datetime
import json
import os
import subprocess
import uuid as uuidlib
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from ..utils import xray
from ..utils.db import connect


router = APIRouter()

HOST = os.getenv("VLESS_HOST", "vpn-gpt.store")
PORT = os.getenv("VLESS_PORT", "2053")


def _error_response(code: str, status: int = 400) -> JSONResponse:
    return JSONResponse(status_code=status, content={"ok": False, "error": code})


def _insert_vpn_key(username: str, uid: str, expires: str, link: str) -> None:
    payload = {
        "username": username,
        "uuid": uid,
        "link": link,
        "issued_at": datetime.datetime.utcnow().strftime("%Y-%m-%d"),
        "expires_at": expires,
        "active": 1,
    }
    fields = ", ".join(payload.keys())
    placeholders = ", ".join(["?"] * len(payload))
    with connect() as conn:
        conn.execute(
            f"INSERT INTO vpn_keys ({fields}) VALUES ({placeholders})",
            tuple(payload.values()),
        )


def _update_expiry(username: str, new_exp: str) -> bool:
    with connect() as conn:
        cur = conn.execute(
            "UPDATE vpn_keys SET expires_at=? WHERE username=? AND active=1",
            (new_exp, username),
        )
        return cur.rowcount > 0


def _deactivate(uuid: str) -> bool:
    with connect() as conn:
        cur = conn.execute("UPDATE vpn_keys SET active=0 WHERE uuid=?", (uuid,))
        return cur.rowcount > 0


def _get_active_key(username: str) -> dict[str, Any] | None:
    with connect() as conn:
        cur = conn.execute(
            "SELECT uuid, expires_at FROM vpn_keys WHERE username=? AND active=1 LIMIT 1",
            (username,),
        )
        row = cur.fetchone()
        return dict(row) if row else None


@router.post("/issue_key")
async def issue_vpn_key(request: Request):
    data = await request.json()
    username = data.get("username")
    if not username or not str(username).strip():
        return _error_response("missing_username")

    try:
        days = int(data.get("days", 30))
    except (TypeError, ValueError):
        return _error_response("invalid_days")
    if days <= 0:
        return _error_response("invalid_days")

    username = str(username).strip()
    uid = str(uuidlib.uuid4())
    expires = (datetime.datetime.utcnow() + datetime.timedelta(days=days)).strftime("%Y-%m-%d")
    link = f"vless://{uid}@{HOST}:{PORT}?encryption=none#{username}"

    _insert_vpn_key(username, uid, expires, link)

    try:
        xray.add_client(username, uid)
    except (FileNotFoundError, json.JSONDecodeError, subprocess.CalledProcessError):
        # Позволяем API работать даже без установленного Xray
        pass

    return {"ok": True, "link": link, "uuid": uid, "expires": expires}


@router.post("/renew_key")
async def renew_vpn_key(request: Request):
    data = await request.json()
    username = data.get("username")
    if not username or not str(username).strip():
        return _error_response("missing_username")

    try:
        days = int(data.get("days", 30))
    except (TypeError, ValueError):
        return _error_response("invalid_days")
    if days <= 0:
        return _error_response("invalid_days")

    username = str(username).strip()
    row = _get_active_key(username)
    if not row:
        return _error_response("user_not_found", status=404)

    new_exp = (
        datetime.datetime.strptime(row["expires_at"], "%Y-%m-%d") + datetime.timedelta(days=days)
    ).strftime("%Y-%m-%d")

    if not _update_expiry(username, new_exp):
        return _error_response("failed_to_update", status=500)

    return {"ok": True, "username": username, "expires": new_exp}


@router.post("/disable_key")
async def disable_vpn_key(request: Request):
    data = await request.json()
    uid = data.get("uuid")
    if not uid or not str(uid).strip():
        return _error_response("missing_uuid")

    uid = str(uid).strip()

    if not _deactivate(uid):
        return _error_response("uuid_not_found", status=404)

    try:
        xray.remove_client(uid)
    except (FileNotFoundError, json.JSONDecodeError, subprocess.CalledProcessError):
        pass

    return {"ok": True, "uuid": uid}
