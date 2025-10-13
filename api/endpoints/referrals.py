from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from api.endpoints.security import require_service_token
from api.utils import db
from api.utils.logging import get_logger

router = APIRouter(prefix="/referral", tags=["referral"])
logger = get_logger("endpoints.referral")


class ReferralUseRequest(BaseModel):
    referrer: str = Field(..., description="Код пригласившего")
    referee: str = Field(..., description="Приглашённый пользователь")
    chat_id: int | None = Field(None)


class ReferralUseResponse(BaseModel):
    ok: bool = True
    referrer: str
    referee: str
    already_exists: bool = False


@router.post("/use", response_model=ReferralUseResponse)
def use_referral(request: ReferralUseRequest, _: None = Depends(require_service_token)):
    try:
        referrer = db.normalise_username(request.referrer)
        referee = db.normalise_username(request.referee)
    except ValueError:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="invalid_username")

    if referrer == referee:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="self_referral")

    existing = db.get_user(referee)
    already_exists = bool(existing and existing.get("referrer"))
    if already_exists and existing["referrer"] != referrer:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="referrer_already_set")

    chat_id = request.chat_id or (existing.get("chat_id") if existing else None) or 0
    db.upsert_user(referee, chat_id, referrer=referrer)
    logger.info("Referral recorded", extra={"referrer": referrer, "referee": referee})
    return ReferralUseResponse(referrer=referrer, referee=referee, already_exists=already_exists)


__all__ = ["use_referral"]
