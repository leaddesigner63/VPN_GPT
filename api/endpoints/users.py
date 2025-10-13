from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from api.endpoints.security import require_service_token
from api.utils import db
from api.utils.logging import get_logger

router = APIRouter(prefix="/users", tags=["users"])
logger = get_logger("endpoints.users")


class RegisterRequest(BaseModel):
    username: str = Field(..., description="Telegram username без @")
    chat_id: int = Field(..., description="Идентификатор чата Telegram")
    referrer: str | None = Field(None, description="Код реферала")


class RegisterResponse(BaseModel):
    ok: bool = True
    username: str
    chat_id: int
    referrer: str | None = None


class KeysResponse(BaseModel):
    ok: bool = True
    username: str
    keys: list[dict]


class ReferralStatsResponse(BaseModel):
    ok: bool = True
    username: str
    total_referrals: int
    total_days: int


@router.post("/register", response_model=RegisterResponse)
def register_user(request: RegisterRequest, _: None = Depends(require_service_token)):
    try:
        username = db.normalise_username(request.username)
    except ValueError:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="invalid_username")

    db.upsert_user(username, request.chat_id, referrer=request.referrer)
    logger.info("Registered Telegram user", extra={"username": username, "chat_id": request.chat_id})
    return RegisterResponse(username=username, chat_id=request.chat_id, referrer=request.referrer)


@router.get("/{username}/keys", response_model=KeysResponse)
def get_user_keys(
    username: str,
    include_inactive: bool = True,
    _: None = Depends(require_service_token),
):
    try:
        normalized = db.normalise_username(username)
    except ValueError:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="invalid_username")

    keys = db.list_user_keys(normalized)
    if not include_inactive:
        keys = [key for key in keys if key.get("active")]

    for key in keys:
        key["trial"] = bool(key.get("trial"))

    logger.info(
        "Returned keys for user",
        extra={"username": normalized, "count": len(keys), "include_inactive": include_inactive},
    )
    return KeysResponse(username=normalized, keys=keys)


@router.get("/{username}/referrals", response_model=ReferralStatsResponse)
def referral_stats(username: str, _: None = Depends(require_service_token)):
    try:
        normalized = db.normalise_username(username)
    except ValueError:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="invalid_username")

    stats = db.get_referral_stats(normalized)
    return ReferralStatsResponse(
        username=normalized,
        total_referrals=int(stats.get("total_referrals", 0)),
        total_days=int(stats.get("total_days", 0)),
    )
