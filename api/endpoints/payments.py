from __future__ import annotations

import datetime as dt
import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from api.config import (
    DEFAULT_COUNTRY,
    REFERRAL_BONUS_DAYS,
    plan_amount,
    plan_duration,
)
from api.endpoints.security import require_service_token
from api.utils import db
from api.utils.logging import get_logger
from api.utils.vless import build_vless_link

router = APIRouter(prefix="/payments", tags=["payments"])
logger = get_logger("endpoints.payments")


class CreatePaymentRequest(BaseModel):
    username: str = Field(...)
    chat_id: int | None = Field(None)
    plan: str = Field(..., description="Код тарифа")
    amount: int | None = Field(None, description="Сумма оплаты, если рассчитывается внешне")


class CreatePaymentResponse(BaseModel):
    ok: bool = True
    payment_id: str
    username: str
    plan: str
    amount: int
    status: str


class ConfirmPaymentRequest(BaseModel):
    payment_id: str
    username: str
    chat_id: int | None = None
    plan: str
    amount: int | None = None
    paid_at: str | None = None


class ConfirmPaymentResponse(BaseModel):
    ok: bool = True
    payment_id: str
    username: str
    plan: str
    amount: int
    status: str
    expires_at: str
    key_uuid: str


def _resolve_amount(plan: str, amount_override: int | None) -> int:
    expected = plan_amount(plan)
    if amount_override is None:
        return expected
    if amount_override != expected:
        logger.warning(
            "Payment amount mismatch",
            extra={"plan": plan, "expected": expected, "actual": amount_override},
        )
    return amount_override


def _ensure_subscription(username: str, chat_id: int | None, days: int) -> dict:
    if chat_id is not None:
        db.upsert_user(username, chat_id)

    existing = db.extend_active_key(username, days=days)
    if existing:
        return existing

    expires_at = dt.datetime.utcnow().replace(microsecond=0) + dt.timedelta(days=days)
    uuid_value = str(uuid.uuid4())
    label = f"VPN_GPT_{username}"
    link = build_vless_link(uuid_value, label)
    return db.create_vpn_key(
        username=username,
        chat_id=chat_id,
        uuid_value=uuid_value,
        link=link,
        expires_at=expires_at,
        label=label,
        country=DEFAULT_COUNTRY,
        trial=False,
    )


def _award_referral_bonus(username: str) -> None:
    referrer = db.get_user_referrer(username)
    if not referrer:
        return
    if db.referral_bonus_exists(referrer, username):
        return

    bonus_days = REFERRAL_BONUS_DAYS
    bonus_key = db.extend_active_key(referrer, days=bonus_days)
    if bonus_key is None:
        referrer_user = db.get_user(referrer)
        chat_id = referrer_user.get("chat_id") if referrer_user else None
        expires_at = dt.datetime.utcnow().replace(microsecond=0) + dt.timedelta(days=bonus_days)
        uuid_value = str(uuid.uuid4())
        label = f"VPN_GPT_{referrer}"
        link = build_vless_link(uuid_value, label)
        db.create_vpn_key(
            username=referrer,
            chat_id=chat_id,
            uuid_value=uuid_value,
            link=link,
            expires_at=expires_at,
            label=label,
            country=DEFAULT_COUNTRY,
            trial=False,
        )
    db.log_referral_bonus(referrer, username, bonus_days)
    logger.info("Awarded referral bonus", extra={"referrer": referrer, "referee": username})


@router.post("/create", response_model=CreatePaymentResponse)
def create_payment(request: CreatePaymentRequest, _: None = Depends(require_service_token)):
    try:
        username = db.normalise_username(request.username)
    except ValueError:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="invalid_username")

    try:
        amount = _resolve_amount(request.plan, request.amount)
    except KeyError:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="unknown_plan")
    payment_id = uuid.uuid4().hex
    record = db.create_payment(
        payment_id=payment_id,
        username=username,
        chat_id=request.chat_id,
        plan=request.plan,
        amount=amount,
    )
    return CreatePaymentResponse(
        payment_id=record["payment_id"],
        username=record["username"],
        plan=record["plan"],
        amount=record["amount"],
        status=record["status"],
    )


@router.post("/confirm", response_model=ConfirmPaymentResponse)
def confirm_payment(request: ConfirmPaymentRequest, _: None = Depends(require_service_token)):
    try:
        username = db.normalise_username(request.username)
    except ValueError:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="invalid_username")

    payment = db.get_payment(request.payment_id)
    if payment is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="payment_not_found")

    if payment["status"] == "paid" and payment.get("key_uuid"):
        logger.info("Payment already confirmed", extra={"payment_id": request.payment_id})
        key = db.get_key_by_uuid(payment["key_uuid"])
        if key is None:
            raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="key_missing")
        return ConfirmPaymentResponse(
            payment_id=payment["payment_id"],
            username=payment["username"],
            plan=payment["plan"],
            amount=payment["amount"],
            status=payment["status"],
            expires_at=key["expires_at"],
            key_uuid=key["uuid"],
        )

    if payment["plan"] != request.plan:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="plan_mismatch")

    try:
        amount = _resolve_amount(request.plan, request.amount or payment["amount"])
        duration_days = plan_duration(request.plan)
    except KeyError:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="unknown_plan")

    subscription = _ensure_subscription(username, request.chat_id, duration_days)
    paid_at = (
        dt.datetime.fromisoformat(request.paid_at)
        if request.paid_at
        else dt.datetime.utcnow().replace(microsecond=0)
    )
    updated = db.update_payment_status(
        request.payment_id,
        status="paid",
        paid_at=paid_at,
        key_uuid=subscription["uuid"],
    )

    if updated is None:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="payment_update_failed")

    _award_referral_bonus(username)

    return ConfirmPaymentResponse(
        payment_id=updated["payment_id"],
        username=updated["username"],
        plan=updated["plan"],
        amount=amount,
        status=updated["status"],
        expires_at=subscription["expires_at"],
        key_uuid=subscription["uuid"],
    )


__all__ = ["create_payment", "confirm_payment"]
