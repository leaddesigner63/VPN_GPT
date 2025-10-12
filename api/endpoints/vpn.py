from __future__ import annotations

import asyncio
import datetime as dt
import os
import random
import uuid
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from api.utils.db import connect
from api.utils.logging import get_logger
from api.utils.vless import build_vless_link
from api.utils import xray

logger = get_logger("endpoints.vpn")

ADMIN_TOKEN = os.getenv("ADMIN_TOKEN")


def require_admin_token(
    authorization: str | None = Header(default=None, alias="Authorization"),
    x_admin_token: str | None = Header(default=None, alias="X-Admin-Token"),
    x_admin_token_query: str | None = Query(default=None, alias="x-admin-token"),
) -> None:
    """Ensure all VPN endpoints are protected with the admin token.

    Historically the API expected a ``Bearer`` token in the ``Authorization`` header,
    while other services in the project relied on the ``X-Admin-Token`` header.  To
    avoid unexpected 401 responses we now accept both variants and validate them
    against the configured ``ADMIN_TOKEN``.
    """

    if not ADMIN_TOKEN:
        logger.error("ADMIN_TOKEN is not configured; denying access")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="admin_token_not_configured"
        )

    candidate_tokens: list[str] = []

    if authorization:
        try:
            scheme, token = authorization.split(" ", 1)
        except ValueError:
            logger.warning("Malformed Authorization header for VPN endpoint")
        else:
            if scheme.lower() == "bearer" and token.strip():
                candidate_tokens.append(token.strip())
            else:
                logger.warning("Unsupported authorization scheme for VPN endpoint")

    if x_admin_token:
        candidate_tokens.append(x_admin_token.strip())

    if x_admin_token_query:
        candidate_tokens.append(x_admin_token_query.strip())

    if not candidate_tokens:
        logger.warning("Missing admin authentication for VPN endpoint")
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="unauthorized")

    if ADMIN_TOKEN not in candidate_tokens:
        logger.warning("Invalid admin token provided for VPN endpoint")
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="unauthorized")


router = APIRouter()
admin_router = APIRouter(dependencies=[Depends(require_admin_token)])


class IssueKeyRequest(BaseModel):
    username: str = Field(..., description="Telegram username без @")
    days: int | None = Field(3, description="Количество дней действия ключа")


class IssueKeyResponse(BaseModel):
    ok: bool = True
    username: str
    uuid: str
    link: str
    expires_at: str
    active: bool = True


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
) -> int:
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
            SET uuid=?, link=?, issued_at=?, expires_at=?, active=0
            WHERE id=?
            """,
            (uuid_value, link, issued_str, expires_str, existing["id"]),
        )
        return existing["id"]
    else:
        cursor = conn.execute(
            """
            INSERT INTO vpn_keys (username, uuid, link, issued_at, expires_at, active)
            VALUES (?, ?, ?, ?, ?, 0)
            """,
            (username, uuid_value, link, issued_str, expires_str),
        )
        return cursor.lastrowid


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


def _format_utc(value: str | None) -> str | None:
    if value is None:
        return None

    try:
        parsed = dt.datetime.fromisoformat(value)
    except ValueError:
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
            try:
                parsed = dt.datetime.strptime(value, fmt)
                break
            except ValueError:
                continue
        else:  # pragma: no cover - defensive
            logger.warning("Failed to parse expires_at value", extra={"value": value})
            return value

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.UTC)
    else:
        parsed = parsed.astimezone(dt.UTC)

    return parsed.isoformat().replace("+00:00", "Z")


def _select_active_key(*, chat_id: int | None = None, username: str | None = None) -> dict[str, Any] | None:
    if chat_id is None and username is None:
        raise ValueError("Either chat_id or username must be provided")

    query = [
        "SELECT username, uuid, link, expires_at, active",
        "FROM vpn_keys",
        "WHERE active = 1",
    ]
    params: tuple[Any, ...]

    if chat_id is not None:
        query.append("AND chat_id = ?")
        params = (chat_id,)
    else:
        query.append("AND username = ?")
        params = (username,)

    query.append("ORDER BY expires_at DESC")
    query.append("LIMIT 1")
    sql = " ".join(query)

    with connect() as conn:
        cur = conn.execute(sql, params)
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


def _normalise_lookup_username(raw: str | None) -> str:
    if raw is None:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail="username_or_chat_id_required")

    username = raw.strip()
    if username.startswith("@"):
        username = username[1:].strip()

    if not username:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail="username_or_chat_id_required")

    return username


async def _sleep_bruteforce_delay() -> None:
    await asyncio.sleep(random.uniform(0.1, 0.2))


@router.get("/my_key", operation_id="getMyKey")
@router.get("/my_key/", operation_id="getMyKeySlash", include_in_schema=False)
async def get_my_key(username: str | None = None, chat_id: int | None = None) -> dict[str, Any]:
    if username is None and chat_id is None:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail="username_or_chat_id_required")

    record: dict[str, Any] | None
    lookup_username: str | None = None

    if chat_id is not None:
        record = _select_active_key(chat_id=chat_id)
    else:
        lookup_username = _normalise_lookup_username(username)
        record = _select_active_key(username=lookup_username)

    if record is None:
        await _sleep_bruteforce_delay()
        logger.warning(
            "Active VPN key not found", extra={"chat_id": chat_id, "username": lookup_username or username}
        )
        return {"ok": False, "error": "not_found"}

    expires_at = _format_utc(record.get("expires_at"))
    response_username = record.get("username") or lookup_username or username

    await _sleep_bruteforce_delay()
    return {
        "ok": True,
        "username": response_username,
        "uuid": record.get("uuid"),
        "link": record.get("link"),
        "expires_at": expires_at,
        "active": bool(record.get("active", 0)),
    }


@admin_router.post(
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
        record_id = _store_vpn_key(
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

        conn.execute("UPDATE vpn_keys SET active=1 WHERE id=?", (record_id,))
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
        active=True,
    )


@admin_router.post("/renew_key", operation_id="renewVpnKey")
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


@admin_router.post("/disable_key", operation_id="disableVpnKey")
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


@admin_router.get("/users/{username}", operation_id="getUserByName")
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


router.include_router(admin_router)

