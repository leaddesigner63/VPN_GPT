from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from api import config
from api.endpoints.security import require_service_token
from api.utils import db
from api.utils.logging import get_logger
from api.utils.vless import build_vless_link

logger = get_logger("endpoints.vpn")

router = APIRouter(prefix="/vpn", tags=["vpn"])


def _normalise_username(raw: str) -> str:
    try:
        return db.normalise_username(raw)
    except ValueError as exc:  # pragma: no cover - invalid input
        logger.warning("Invalid username received", extra={"error": str(exc)})
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="invalid_username")


def _json_error(code: str, *, status_code: int = status.HTTP_400_BAD_REQUEST) -> JSONResponse:
    logger.warning("Returning error response", extra={"code": code, "status": status_code})
    return JSONResponse(status_code=status_code, content={"ok": False, "error": code})


class IssueKeyRequest(BaseModel):
    username: str = Field(..., description="Telegram username без @")
    chat_id: int | None = Field(None, description="Telegram chat identifier")
    trial: bool = Field(True, description="Разрешить выдачу тестового периода")
    country: str | None = Field(None, description="Код выбранной страны")
    label: str | None = Field(None, description="Метка для VLESS ссылки")


class RenewKeyRequest(BaseModel):
    username: str = Field(...)
    plan: str | None = Field(None, description="Код тарифа (1m,3m,12m)")
    days: int | None = Field(None, description="Произвольное продление в днях")
    chat_id: int | None = Field(None)
    country: str | None = Field(None)
    label: str | None = Field(None)


class KeyResponse(BaseModel):
    ok: bool = True
    username: str
    uuid: str
    link: str
    expires_at: str
    trial: bool = False
    reused: bool = False


def _build_key_response(payload: dict[str, Any], *, reused: bool = False) -> KeyResponse:
    return KeyResponse(
        username=payload["username"],
        uuid=payload["uuid"],
        link=payload["link"],
        expires_at=payload["expires_at"],
        trial=bool(payload.get("trial", False)),
        reused=reused,
    )


@router.post("/issue_key", response_model=KeyResponse)
def issue_key(request: IssueKeyRequest, _: None = Depends(require_service_token)):
    username = _normalise_username(request.username)
    if request.chat_id is not None:
        db.upsert_user(username, request.chat_id)

    existing = db.get_active_key(username)
    if existing:
        logger.info("Returning existing active key", extra={"username": username})
        return _build_key_response(existing, reused=True)

    if not request.trial or config.TRIAL_DAYS <= 0:
        logger.info("Trial disabled", extra={"username": username})
        return _json_error("trial_unavailable", status_code=status.HTTP_409_CONFLICT)

    if db.user_has_trial(username):
        logger.info("User already consumed trial", extra={"username": username})
        return _json_error("trial_already_used", status_code=status.HTTP_409_CONFLICT)

    expires_at = dt.datetime.utcnow().replace(microsecond=0) + dt.timedelta(days=config.TRIAL_DAYS)
    uuid_value = str(uuid.uuid4())
    label = request.label or f"VPN_GPT_{username}"
    link = build_vless_link(uuid_value, label)
    payload = db.create_vpn_key(
        username=username,
        chat_id=request.chat_id,
        uuid_value=uuid_value,
        link=link,
        expires_at=expires_at,
        label=label,
        country=request.country or config.DEFAULT_COUNTRY,
        trial=True,
    )
    return _build_key_response(payload)


def _resolve_duration(plan: str | None, days: int | None) -> tuple[int, str | None]:
    from api.config import plan_duration

    if plan:
        return plan_duration(plan), plan
    if days:
        return days, None
    raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="plan_or_days_required")


@router.post("/renew_key", response_model=KeyResponse)
def renew_key(request: RenewKeyRequest, _: None = Depends(require_service_token)):
    username = _normalise_username(request.username)
    if request.chat_id is not None:
        db.upsert_user(username, request.chat_id)

    duration_days, plan_code = _resolve_duration(request.plan, request.days)
    existing = db.extend_active_key(username, days=duration_days)
    if existing:
        logger.info(
            "Extended existing key",
            extra={"username": username, "days": duration_days, "plan": plan_code},
        )
        return _build_key_response(existing)

    uuid_value = str(uuid.uuid4())
    label = request.label or f"VPN_GPT_{username}"
    link = build_vless_link(uuid_value, label)
    expires_at = dt.datetime.utcnow().replace(microsecond=0) + dt.timedelta(days=duration_days)
    payload = db.create_vpn_key(
        username=username,
        chat_id=request.chat_id,
        uuid_value=uuid_value,
        link=link,
        expires_at=expires_at,
        label=label,
        country=request.country or config.DEFAULT_COUNTRY,
        trial=False,
    )
    logger.info(
        "Created new key during renewal",
        extra={"username": username, "days": duration_days, "plan": plan_code},
    )
    return _build_key_response(payload)
