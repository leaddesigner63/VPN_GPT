from __future__ import annotations

import datetime as dt
import os
import uuid
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from api.utils.db import connect
from api.utils.logging import get_logger
from api.utils.vless import build_vless_link
from api.utils import xray

logger = get_logger("endpoints.vpn")

ADMIN_TOKEN = os.getenv("ADMIN_TOKEN")


def require_admin_token(authorization: str | None = Header(default=None, alias="Authorization")) -> None:
    """Ensure all VPN endpoints are protected with the admin bearer token."""

    if not ADMIN_TOKEN:
        logger.error("ADMIN_TOKEN is not configured; denying access")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="admin_token_not_configured")

    if not authorization:
        logger.warning("Missing Authorization header for VPN endpoint")
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="unauthorized")

    try:
        scheme, token = authorization.split(" ", 1)
    except ValueError:
        logger.warning("Malformed Authorization header for VPN endpoint")
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="unauthorized")

    if scheme.lower() != "bearer" or token.strip() != ADMIN_TOKEN:
        logger.warning("Invalid admin token provided for VPN endpoint")
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="unauthorized")


router = APIRouter(dependencies=[Depends(require_admin_token)])


class IssueKeyRequest(BaseModel):
    username: str = Field(..., description="Telegram username без @")
    days: int | None = Field(3, description="Количество дней действия ключа")


class IssueKeyResponse(BaseModel):
    ok: bool = True
    username: str
    uuid: str
    link: str
    expires_at: str


class RenewKeyRequest(BaseModel):
    username: str
    days: int = 30


class DisableKeyRequest(BaseModel):
    uuid: str


def _normalise_username(raw: str | None) -> str:
    if raw is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="missing_username")

    username = raw.strip()
    if username.startswith("@"):
        username = username[1:].strip()

    if not username:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="missing_username")

    return username


def _json_error(code: str, status_code: int = status.HTTP_400_BAD_REQUEST) -> JSONResponse:
    logger.warning("Returning error response", extra={"error": code, "status": status_code})
    return JSONResponse(status_code=status_code, content={"ok": False, "error": code})


def _store_vpn_key(
    conn,
    *,
    username: str,
    uuid_value: str,
    link: str,
    issued_at: dt.datetime,
    expires_at: dt.datetime,
) -> None:
    existing = conn.execute(
        "SELECT id FROM vpn_keys WHERE username=? LIMIT 1",
        (username,),
    ).fetchone()

    expires_str = expires_at.isoformat()
    issued_str = issued_at.isoformat()

    if existing:
        conn.execute(
            """
            UPDATE vpn_keys
            SET uuid=?, link=?, issued_at=?, expires_at=?, active=1
            WHERE id=?
            """,
            (uuid_value, link, issued_str, expires_str, existing["id"]),
        )
    else:
        conn.execute(
            """
            INSERT INTO vpn_keys (username, uuid, link, issued_at, expires_at, active)
            VALUES (?, ?, ?, ?, ?, 1)
            """,
            (username, uuid_value, link, issued_str, expires_str),
        )


def _parse_date(value: str) -> dt.datetime:
    try:
        return dt.datetime.fromisoformat(value)
    except ValueError:
        return dt.datetime.strptime(value, "%Y-%m-%d")


def _get_active_key(username: str) -> dict[str, Any] | None:
    with connect() as conn:
        cur = conn.execute(
            "SELECT uuid, expires_at, link FROM vpn_keys WHERE username=? AND active=1 LIMIT 1",
            (username,),
        )
        row = cur.fetchone()
        return None if row is None else dict(row)


def _update_expiry(username: str, new_exp: dt.datetime) -> bool:
    with connect() as conn:
        cur = conn.execute(
            "UPDATE vpn_keys SET expires_at=? WHERE username=? AND active=1",
            (new_exp.isoformat(), username),
        )
        return cur.rowcount > 0


def _deactivate(uuid_value: str) -> bool:
    with connect() as conn:
        cur = conn.execute("UPDATE vpn_keys SET active=0 WHERE uuid=?", (uuid_value,))
        return cur.rowcount > 0


@router.post(
    "/issue_key",
    operation_id="issueVpnKey",
    response_model=IssueKeyResponse,
)
def issue_vpn_key(payload: IssueKeyRequest) -> IssueKeyResponse:
    username = _normalise_username(payload.username)

    days = payload.days if payload.days is not None else 3
    if days <= 0:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="invalid_days")

    issued_at = dt.datetime.now(dt.UTC)
    expires_at = issued_at + dt.timedelta(days=days)
    uuid_value = str(uuid.uuid4())
    link = build_vless_link(uuid_value, username)

    with connect(autocommit=False) as conn:
        _store_vpn_key(
            conn,
            username=username,
            uuid_value=uuid_value,
            link=link,
            issued_at=issued_at,
            expires_at=expires_at,
        )

        try:
            xray.add_client_no_duplicates(uuid_value, username)
        except xray.XrayRestartError:
            conn.rollback()
            logger.exception(
                "Xray restart failed after issuing key", extra={"username": username, "uuid": uuid_value}
            )
            return _json_error("xray_restart_failed", status.HTTP_500_INTERNAL_SERVER_ERROR)
        except Exception:  # pragma: no cover - defensive logging
            conn.rollback()
            logger.exception(
                "Unexpected Xray synchronisation failure", extra={"username": username, "uuid": uuid_value}
            )
            raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="failed_to_sync_xray")

        conn.commit()

    logger.info(
        "Issued VPN key",
        extra={"username": username, "uuid": uuid_value, "expires_at": expires_at.isoformat()},
    )
    return IssueKeyResponse(
        ok=True,
        username=username,
        uuid=uuid_value,
        link=link,
        expires_at=expires_at.isoformat(),
    )


@router.post("/renew_key", operation_id="renewVpnKey")
def renew_vpn_key(payload: RenewKeyRequest) -> dict[str, Any]:
    username = _normalise_username(payload.username)

    if payload.days <= 0:
        return _json_error("invalid_days")

    current = _get_active_key(username)
    if not current:
        return _json_error("user_not_found", status.HTTP_404_NOT_FOUND)

    try:
        current_exp = _parse_date(current["expires_at"])
    except Exception:  # pragma: no cover - defensive conversion
        current_exp = dt.datetime.now(dt.UTC)

    new_exp = current_exp + dt.timedelta(days=payload.days)
    if not _update_expiry(username, new_exp):
        return _json_error("failed_to_update", status.HTTP_500_INTERNAL_SERVER_ERROR)

    return {"ok": True, "username": username, "expires_at": new_exp.isoformat()}


@router.post("/disable_key", operation_id="disableVpnKey")
def disable_vpn_key(payload: DisableKeyRequest) -> dict[str, Any]:
    uuid_value = payload.uuid.strip()
    if not uuid_value:
        return _json_error("missing_uuid")

    if not _deactivate(uuid_value):
        return _json_error("uuid_not_found", status.HTTP_404_NOT_FOUND)

    try:
        xray.remove_client(uuid_value)
    except xray.XrayError:  # pragma: no cover - logging inside helper
        logger.exception("Failed to remove client from Xray", extra={"uuid": uuid_value})

    return {"ok": True, "uuid": uuid_value}


@router.get("/users/{username}", operation_id="getUserByName")
def get_user(username: str) -> dict[str, Any]:
    username = _normalise_username(username)
    key = _get_active_key(username)
    if not key:
        return {"ok": True, "username": username, "keys": []}
    return {
        "ok": True,
        "username": username,
        "keys": [
            {
                "uuid": key["uuid"],
                "active": True,
                "expires_at": key["expires_at"],
                "link": key["link"],
            }
        ],
    }

