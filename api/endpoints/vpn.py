from __future__ import annotations

import datetime
import json
import os
import sqlite3
import subprocess
import uuid as uuidlib
import fcntl
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from api.utils import xray
from ..utils.env import get_vless_host
from ..utils.db import connect
from api.utils.link import compose_vless_link
from api.utils.logging import get_logger

router = APIRouter()

HOST = get_vless_host()
PORT = os.getenv("VLESS_PORT", "2053")

logger = get_logger("endpoints.vpn")


# === Helpers ===
def _error_response(code: str, status: int = 400) -> JSONResponse:
    logger.warning("Returning error response", extra={"error": code, "status": status})
    return JSONResponse(status_code=status, content={"ok": False, "error": code})


def _insert_vpn_key(username: str, uid: str, expires: str, link: str) -> bool:
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
    try:
        with connect() as conn:
            conn.execute(
                f"INSERT INTO vpn_keys ({fields}) VALUES ({placeholders})",
                tuple(payload.values()),
            )
    except sqlite3.IntegrityError:
        logger.info(
            "Existing VPN record detected, attempting recovery",
            extra={"username": username},
        )

        with connect() as conn:
            row = conn.execute(
                "SELECT id, active, uuid, link FROM vpn_keys WHERE username=?",
                (username,),
            ).fetchone()

            if not row:
                logger.warning(
                    "Integrity error without matching row",
                    extra={"username": username},
                )
                return False

            if row["active"] == 1 and row["uuid"] and row["link"]:
                logger.warning(
                    "Duplicate VPN key insertion prevented",
                    extra={"username": username, "uuid": row["uuid"]},
                )
                return False

            conn.execute(
                """
                UPDATE vpn_keys
                SET uuid=?, link=?, issued_at=?, expires_at=?, active=1
                WHERE id=?
                """,
                (
                    uid,
                    link,
                    payload["issued_at"],
                    payload["expires_at"],
                    row["id"],
                ),
            )
        logger.info(
            "Reused existing VPN record for user",
            extra={"username": username, "uuid": uid},
        )
        return True

    logger.info("Inserted new VPN key", extra={"username": username, "uuid": uid, "expires": expires})
    return True


def _update_expiry(username: str, new_exp: str) -> bool:
    with connect() as conn:
        cur = conn.execute(
            "UPDATE vpn_keys SET expires_at=? WHERE username=? AND active=1",
            (new_exp, username),
        )
        updated = cur.rowcount > 0
    if updated:
        logger.info("Updated VPN key expiry", extra={"username": username, "expires": new_exp})
    else:
        logger.warning("Attempted to renew non-existing VPN key", extra={"username": username})
    return updated


def _deactivate(uuid: str) -> bool:
    with connect() as conn:
        cur = conn.execute("UPDATE vpn_keys SET active=0 WHERE uuid=?", (uuid,))
        deactivated = cur.rowcount > 0
    if deactivated:
        logger.info("Deactivated VPN key", extra={"uuid": uuid})
    else:
        logger.warning("Attempted to deactivate unknown VPN key", extra={"uuid": uuid})
    return deactivated


def _get_active_key(username: str) -> dict[str, Any] | None:
    with connect() as conn:
        cur = conn.execute(
            "SELECT uuid, expires_at FROM vpn_keys WHERE username=? AND active=1 LIMIT 1",
            (username,),
        )
        row = cur.fetchone()
        if row:
            logger.debug("Located active VPN key for %s", username)
            return dict(row)
        logger.debug("No active VPN key found for %s", username)
        return None


# === Core logic ===
def _safe_add_client(username: str, uid: str) -> None:
    """Add a VLESS client to Xray with full duplicate cleanup and file lock."""
    config_path = "/usr/local/etc/xray/config.json"
    try:
        with open(config_path, "r+", encoding="utf-8") as f:
            # блокировка файла от одновременной записи
            fcntl.flock(f, fcntl.LOCK_EX)
            config = json.load(f)
            clients = config["inbounds"][0]["settings"]["clients"]

            # очистка дубликатов по email
            seen = set()
            unique_clients = []
            for c in clients:
                email = c.get("email")
                if email not in seen:
                    seen.add(email)
                    unique_clients.append(c)
                else:
                    logger.warning("Removed duplicate email from config: %s", email)

            config["inbounds"][0]["settings"]["clients"] = unique_clients

            # если пользователь уже есть — не добавляем повторно
            if any(c.get("email") == username for c in unique_clients):
                logger.info("Client %s already exists — skipping duplicate.", username)
            else:
                unique_clients.append({"id": uid, "level": 0, "email": username})
                logger.info("Added new Xray client %s", username)

            # записываем обновлённый конфиг
            f.seek(0)
            json.dump(config, f, indent=2)
            f.truncate()
            fcntl.flock(f, fcntl.LOCK_UN)

        # перезапуск Xray только после успешного обновления
        result = subprocess.run(["systemctl", "restart", "xray"], check=False)
        if result.returncode == 0:
            logger.info("Xray restarted successfully.")
        else:
            logger.warning("Xray restart returned code %s", result.returncode)

    except Exception as err:
        logger.exception("Failed to safely update Xray config", extra={"error": str(err)})


# === API endpoints ===
@router.post(
    "/issue_key",
    summary="Выдать VPN-ключ",
    description="Выдаёт VPN-ключ бесплатно (временный демо-режим)",
)
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
    logger.info("Issuing VPN key", extra={"username": username, "days": days})
    active_key = _get_active_key(username)
    if active_key:
        logger.warning(
            "User already has active key — skipping new issue.",
            extra={"username": username, "uuid": active_key.get("uuid")},
        )
        return JSONResponse(
            status_code=409,
            content={
                "ok": False,
                "error": "active_key_exists",
                "message": "У пользователя уже есть действующий ключ.",
            },
        )

    uid = str(uuidlib.uuid4())
    expires = (datetime.datetime.utcnow() + datetime.timedelta(days=days)).strftime("%Y-%m-%d")
    link = compose_vless_link(uid, username)

    if not _insert_vpn_key(username, uid, expires, link):
        return _error_response("user_already_has_key", status=409)

    # Добавляем клиента безопасно
    _safe_add_client(username, uid)

    return {
        "ok": True,
        "link": link,
        "uuid": uid,
        "expires": expires,
        "message": "Ключ создан успешно.",
    }


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
    logger.info("Renewing VPN key", extra={"username": username, "days": days})
    row = _get_active_key(username)
    if not row:
        return _error_response("user_not_found", status=404)

    new_exp = (
        datetime.datetime.strptime(row["expires_at"], "%Y-%m-%d")
        + datetime.timedelta(days=days)
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
    logger.info("Disabling VPN key", extra={"uuid": uid})

    if not _deactivate(uid):
        return _error_response("uuid_not_found", status=404)

    try:
        xray.remove_client(uid)
    except (FileNotFoundError, json.JSONDecodeError, subprocess.CalledProcessError) as err:
        logger.exception("Failed to remove client from Xray", extra={"uuid": uid, "error": str(err)})

    return {"ok": True, "uuid": uid}
