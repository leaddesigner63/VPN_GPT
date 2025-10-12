from __future__ import annotations

import datetime
import os
import uuid as uuidlib
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, EmailStr, TypeAdapter, ValidationError

from api.utils import xray
from ..utils.db import connect
from api.utils.link import compose_vless_link
from api.utils.logging import get_logger

router = APIRouter()

logger = get_logger("endpoints.vpn")

ADMIN_TOKEN = os.getenv("ADMIN_TOKEN")


class IssueKeyPayload(BaseModel):
    email: EmailStr


class IssueKeyResponse(BaseModel):
    email: EmailStr
    key: str
    created: bool


class KeyResponse(BaseModel):
    email: EmailStr
    key: str


class BulkIssueReport(BaseModel):
    issued: int
    skipped: int


EMAIL_ADAPTER = TypeAdapter(EmailStr)


def _normalise_email(raw_email: str) -> str:
    """Validate and normalise an e-mail string to lowercase."""

    value = (raw_email or "").strip()
    if not value:
        raise HTTPException(status_code=400, detail="email is required")
    try:
        email = EMAIL_ADAPTER.validate_python(value)
    except ValidationError as exc:
        logger.warning("Rejected invalid email", extra={"email": raw_email})
        raise HTTPException(status_code=400, detail="invalid email format") from exc
    return email.lower()


def _error_response(code: str, status: int = 400) -> JSONResponse:
    logger.warning("Returning error response", extra={"error": code, "status": status})
    return JSONResponse(status_code=status, content={"ok": False, "error": code})


def _compose_link_safe(uuid: str, email: str) -> str | None:
    """Compose a VLESS link while tolerating configuration errors."""

    try:
        return compose_vless_link(uuid, email)
    except Exception as err:  # pragma: no cover - defensive logging
        logger.exception(
            "Failed to compose VLESS link", extra={"email": email, "uuid": uuid}
        )
        return None


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
# === API endpoints ===
@router.post(
    "/issue_key",
    summary="Выдать VPN-ключ",
    description="Создаёт VPN-ключ для клиента или возвращает существующий.",
    response_model=IssueKeyResponse,
    responses={
        400: {
            "description": "Некорректный запрос",
            "content": {
                "application/json": {
                    "examples": {
                        "missing": {"value": {"detail": "email is required"}},
                        "invalid": {
                            "value": {"detail": "invalid email format"}
                        },
                    }
                }
            },
        },
        409: {
            "description": "Конфликт уникальности email",
            "content": {
                "application/json": {
                    "examples": {
                        "conflict": {
                            "value": {"detail": "email conflict"}
                        }
                    }
                }
            },
        },
        502: {
            "description": "Ошибка интеграции с Xray",
            "content": {
                "application/json": {
                    "examples": {
                        "xray": {
                            "value": {"detail": "failed to sync key with xray"}
                        }
                    }
                }
            },
        },
    },
)
async def issue_vpn_key(request: Request) -> IssueKeyResponse:
    try:
        payload = await request.json()
    except ValueError as exc:
        logger.warning("Invalid JSON body for key issuance")
        raise HTTPException(status_code=400, detail="invalid json body") from exc

    if not isinstance(payload, dict):
        logger.warning("Unexpected payload type for key issuance", extra={"type": type(payload).__name__})
        raise HTTPException(status_code=400, detail="invalid request body")

    raw_email = payload.get("email")
    if raw_email is None:
        raise HTTPException(status_code=400, detail="email is required")

    email = _normalise_email(str(raw_email))
    logger.info("Processing VPN key issuance", extra={"email": email})

    with connect(autocommit=False) as conn:
        row = conn.execute(
            "SELECT id, uuid, active FROM vpn_keys WHERE LOWER(username)=? LIMIT 1",
            (email,),
        ).fetchone()

        if row and row["uuid"]:
            logger.info(
                "Returning existing VPN key", extra={"email": email, "uuid": row["uuid"]}
            )
            conn.commit()
            return IssueKeyResponse(email=email, key=row["uuid"], created=False)

        key = str(uuidlib.uuid4())
        issued_at = datetime.datetime.now(datetime.UTC).isoformat()
        link = _compose_link_safe(key, email)

        if row:
            conn.execute(
                """
                UPDATE vpn_keys
                SET uuid=?, link=?, issued_at=?, active=1, expires_at=NULL, username=?
                WHERE id=?
                """,
                (key, link, issued_at, email, row["id"]),
            )
        else:
            conn.execute(
                """
                INSERT INTO vpn_keys (username, uuid, link, issued_at, active)
                VALUES (?, ?, ?, ?, 1)
                """,
                (email, key, link, issued_at),
            )

        try:
            xray.add_client(email=email, client_id=key)
        except Exception as exc:  # pragma: no cover - relies on system state
            conn.rollback()
            logger.exception(
                "Failed to synchronise key with Xray", extra={"email": email, "uuid": key}
            )
            raise HTTPException(status_code=502, detail="failed to sync key with xray") from exc

        try:
            conn.commit()
        except Exception as db_exc:  # pragma: no cover - defensive cleanup
            conn.rollback()
            logger.exception(
                "Database commit failed after key issuance", extra={"email": email, "uuid": key}
            )
            try:
                xray.remove_client(key)
            except Exception:
                logger.exception(
                    "Failed to rollback Xray client after DB error",
                    extra={"email": email, "uuid": key},
                )
            raise HTTPException(status_code=500, detail="failed to store key") from db_exc

    logger.info("Issued new VPN key", extra={"email": email, "uuid": key})
    return IssueKeyResponse(email=email, key=key, created=True)


@router.get(
    "/key",
    summary="Получить VPN-ключ",
    response_model=KeyResponse,
    responses={
        400: {
            "description": "Некорректный email",
            "content": {
                "application/json": {
                    "examples": {
                        "invalid": {
                            "value": {"detail": "invalid email format"}
                        }
                    }
                }
            },
        },
        404: {
            "description": "Ключ не найден",
            "content": {
                "application/json": {
                    "examples": {
                        "missing": {"value": {"detail": "key not found"}}
                    }
                }
            },
        },
    },
)
def get_vpn_key(email: str = Query(..., description="Email клиента")) -> KeyResponse:
    email = _normalise_email(email)
    with connect() as conn:
        row = conn.execute(
            """
            SELECT uuid
            FROM vpn_keys
            WHERE active=1 AND uuid IS NOT NULL AND TRIM(uuid)<>'' AND LOWER(username)=?
            LIMIT 1
            """,
            (email,),
        ).fetchone()

    if not row or not row["uuid"]:
        logger.info("VPN key lookup failed", extra={"email": email})
        raise HTTPException(status_code=404, detail="key not found")

    logger.info("VPN key lookup succeeded", extra={"email": email, "uuid": row["uuid"]})
    return KeyResponse(email=email, key=row["uuid"])


@router.post(
    "/issue_missing_keys",
    summary="Выдать отсутствующие VPN-ключи",
    response_model=BulkIssueReport,
    responses={
        401: {
            "description": "Требуется авторизация",
            "content": {
                "application/json": {
                    "examples": {
                        "unauthorized": {"value": {"detail": "unauthorized"}}
                    }
                }
            },
        },
        502: {
            "description": "Ошибка интеграции с Xray",
            "content": {
                "application/json": {
                    "examples": {
                        "xray": {
                            "value": {"detail": "failed to sync keys with xray"}
                        }
                    }
                }
            },
        },
    },
)
def issue_missing_keys(
    x_admin_token: str | None = Header(default=None, alias="X-Admin-Token")
) -> BulkIssueReport:
    if not ADMIN_TOKEN or x_admin_token != ADMIN_TOKEN:
        logger.warning("Unauthorized bulk key issuance attempt")
        raise HTTPException(status_code=401, detail="unauthorized")

    logger.info("Starting bulk issuance of missing VPN keys")
    issued = 0
    skipped = 0
    keys_for_xray: list[tuple[str, str]] = []

    with connect(autocommit=False) as conn:
        rows = conn.execute(
            """
            SELECT id, username
            FROM vpn_keys
            WHERE (uuid IS NULL OR TRIM(uuid)='')
              AND username IS NOT NULL AND TRIM(username)<>''
            ORDER BY id
            """
        ).fetchall()

        if not rows:
            conn.commit()
            return BulkIssueReport(issued=0, skipped=0)

        now = datetime.datetime.now(datetime.UTC).isoformat()

        for row in rows:
            candidate = row["username"]
            try:
                email = _normalise_email(candidate)
            except HTTPException:
                skipped += 1
                continue

            duplicate = conn.execute(
                "SELECT 1 FROM vpn_keys WHERE LOWER(username)=? AND id<>?",
                (email, row["id"]),
            ).fetchone()
            if duplicate:
                skipped += 1
                continue

            key = str(uuidlib.uuid4())
            link = _compose_link_safe(key, email)
            conn.execute(
                """
                UPDATE vpn_keys
                SET username=?, uuid=?, link=?, issued_at=?, active=1, expires_at=NULL
                WHERE id=?
                """,
                (email, key, link, now, row["id"]),
            )
            keys_for_xray.append((email, key))
            issued += 1

        if not keys_for_xray:
            conn.commit()
            return BulkIssueReport(issued=0, skipped=skipped)

        added: list[str] = []
        try:
            for email, key in keys_for_xray:
                xray.add_client(email=email, client_id=key)
                added.append(key)
        except Exception as exc:  # pragma: no cover - depends on system state
            conn.rollback()
            for uuid in reversed(added):
                try:
                    xray.remove_client(uuid)
                except Exception:
                    logger.exception(
                        "Failed to rollback Xray client after bulk sync error",
                        extra={"uuid": uuid},
                    )
            logger.exception("Bulk Xray synchronisation failed")
            raise HTTPException(status_code=502, detail="failed to sync keys with xray") from exc

        try:
            conn.commit()
        except Exception as db_exc:  # pragma: no cover - defensive cleanup
            conn.rollback()
            for uuid in reversed(keys_for_xray):
                try:
                    xray.remove_client(uuid[1])
                except Exception:
                    logger.exception(
                        "Failed to rollback Xray client after DB error",
                        extra={"uuid": uuid[1]},
                    )
            raise HTTPException(status_code=500, detail="failed to store keys") from db_exc

    logger.info(
        "Bulk issuance complete", extra={"issued": issued, "skipped": skipped}
    )
    return BulkIssueReport(issued=issued, skipped=skipped)


issue_vpn_key.openapi_extra = {
    "requestBody": {
        "required": True,
        "content": {
            "application/json": {
                "schema": IssueKeyPayload.model_json_schema(),
                "examples": {
                    "new": {
                        "summary": "Создание ключа",
                        "value": {"email": "user@example.com"},
                    }
                },
            }
        },
    }
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
