from __future__ import annotations

import datetime as dt
import decimal
import json
import uuid
from collections.abc import Mapping, Sequence
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from pydantic import BaseModel, Field

from api import config
from api.config import (
    DEFAULT_COUNTRY,
    MORUNE_DEFAULT_CURRENCY,
    MORUNE_FAIL_URL,
    MORUNE_HOOK_URL,
    MORUNE_SUCCESS_URL,
    PAYMENTS_DEFAULT_SOURCE,
    PAYMENTS_PUBLIC_TOKEN,
    REFERRAL_BONUS_DAYS,
    plan_amount,
    plan_duration,
)
from api.endpoints.security import require_service_token
from api.integrations.morune import (
    MoruneAPIError,
    MoruneClient,
    MoruneConfigurationError,
    MoruneSignatureError,
)
from api.utils import db
from api.utils.logging import get_logger
from api.utils.vless import build_vless_link

router = APIRouter(prefix="/payments", tags=["payments"])
logger = get_logger("endpoints.payments")

_MORUNE_CLIENT: MoruneClient | None = None
_MORUNE_CLIENT_SETTINGS: tuple[str, str, str, str] | None = None


def _serialise_metadata_value(value: Any) -> Any:
    """Convert metadata values into JSON-serialisable primitives."""

    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (dt.datetime, dt.date)):
        return value.isoformat()
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, bytes):
        import base64

        return base64.b64encode(value).decode("ascii")
    if isinstance(value, decimal.Decimal):
        return format(value, "f")
    if isinstance(value, Mapping):
        return {str(key): _serialise_metadata_value(val) for key, val in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_serialise_metadata_value(item) for item in value]
    return str(value)


def _get_morune_client() -> MoruneClient | None:
    global _MORUNE_CLIENT, _MORUNE_CLIENT_SETTINGS

    api_key = config.MORUNE_API_KEY or ""
    shop_id = config.MORUNE_SHOP_ID or ""
    base_url = config.MORUNE_BASE_URL
    webhook_secret = config.MORUNE_WEBHOOK_SECRET or ""

    if not (api_key and shop_id):
        if _MORUNE_CLIENT is not None:
            _MORUNE_CLIENT.close()
        _MORUNE_CLIENT = None
        _MORUNE_CLIENT_SETTINGS = None
        return None

    desired_settings = (api_key, shop_id, base_url, webhook_secret)

    if _MORUNE_CLIENT is not None and _MORUNE_CLIENT_SETTINGS == desired_settings:
        return _MORUNE_CLIENT
    try:
        _MORUNE_CLIENT = MoruneClient(
            base_url=base_url,
            api_key=api_key,
            shop_id=shop_id,
            webhook_secret=webhook_secret or None,
        )
        _MORUNE_CLIENT_SETTINGS = desired_settings
        logger.info("Morune client initialised", extra={"base_url": base_url})
    except MoruneConfigurationError:
        logger.warning("Morune configuration incomplete; client disabled")
        _MORUNE_CLIENT = None
        _MORUNE_CLIENT_SETTINGS = None
    return _MORUNE_CLIENT


class CreatePaymentRequest(BaseModel):
    username: str = Field(...)
    chat_id: int | None = Field(None)
    plan: str = Field(..., description="Код тарифа")
    amount: int | None = Field(None, description="Сумма оплаты, если рассчитывается внешне")
    currency: str | None = Field(None, description="Код валюты ISO 4217")
    referrer: str | None = Field(None, description="Реферальный идентификатор")
    metadata: dict[str, Any] | None = Field(None, description="Дополнительные метаданные платежа")
    source: str | None = Field(None, description="Источник (telegram/api/site)")


class CreatePaymentResponse(BaseModel):
    ok: bool = True
    payment_id: str
    username: str
    plan: str
    amount: int
    currency: str
    status: str
    provider: str | None = None
    provider_payment_id: str | None = None
    payment_url: str | None = None


class PublicCreatePaymentRequest(BaseModel):
    username: str
    plan: str
    chat_id: int | None = None
    referrer: str | None = None
    amount: int | None = None
    currency: str | None = None
    metadata: dict[str, Any] | None = None


class ConfirmPaymentRequest(BaseModel):
    payment_id: str
    username: str
    chat_id: int | None = None
    plan: str
    amount: int | None = None
    paid_at: str | None = None
    currency: str | None = None
    provider_status: str | None = None
    payment_url: str | None = None
    raw_provider_payload: dict[str, Any] | None = None


class ConfirmPaymentResponse(BaseModel):
    ok: bool = True
    payment_id: str
    username: str
    plan: str
    amount: int
    currency: str
    status: str
    expires_at: str
    key_uuid: str
    payment_url: str | None = None


class WebhookAck(BaseModel):
    ok: bool
    payment_id: str | None = None
    detail: str | None = None


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


def _prepare_metadata(
    *,
    payment_id: str,
    username: str,
    plan: str,
    source: str,
    chat_id: int | None,
    referrer: str | None,
    metadata: dict[str, Any] | None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    if metadata:
        for key, value in metadata.items():
            payload[str(key)] = _serialise_metadata_value(value)
    payload.update(
        {
            "order_id": payment_id,
            "username": username,
            "plan": plan,
            "source": source,
        }
    )
    if chat_id is not None:
        payload["chat_id"] = chat_id
    if referrer:
        payload["referrer"] = referrer
    return payload


def _create_payment_internal(
    *,
    username: str,
    chat_id: int | None,
    plan: str,
    amount_override: int | None,
    currency: str | None,
    referrer: str | None,
    metadata: dict[str, Any] | None,
    source: str,
) -> CreatePaymentResponse:
    try:
        normalized_username = db.normalise_username(username)
    except ValueError:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="invalid_username")

    normalized_referrer: str | None = None
    if referrer is not None:
        try:
            normalized_referrer = db.normalise_username(referrer)
        except ValueError:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="invalid_referrer")

    try:
        amount = _resolve_amount(plan, amount_override)
    except KeyError:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="unknown_plan")

    resolved_currency = (currency or MORUNE_DEFAULT_CURRENCY or "RUB").upper()
    payment_id = uuid.uuid4().hex
    metadata_payload = _prepare_metadata(
        payment_id=payment_id,
        username=normalized_username,
        plan=plan,
        source=source,
        chat_id=chat_id,
        referrer=normalized_referrer,
        metadata=metadata,
    )

    provider = None
    provider_payment_id = None
    payment_url = None
    provider_status = None
    raw_payload: dict[str, Any] | None = None

    client = _get_morune_client()
    if client:
        try:
            custom_fields = json.dumps(
                {
                    "plan": plan,
                    "username": normalized_username,
                },
                ensure_ascii=False,
                separators=(",", ":"),
            )
            invoice = client.create_invoice(
                payment_id=payment_id,
                amount=amount,
                currency=resolved_currency,
                comment=f"VPN_GPT {plan}",
                success_url=MORUNE_SUCCESS_URL,
                fail_url=MORUNE_FAIL_URL,
                hook_url=MORUNE_HOOK_URL,
                include_service=None,
                expire=300,
                custom_fields=custom_fields,
            )
        except MoruneAPIError as exc:
            logger.exception("Failed to create Morune invoice", extra={"payment_id": payment_id})
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="morune_create_failed",
            ) from exc

        provider = "morune"
        provider_payment_id = invoice.provider_payment_id
        payment_url = invoice.payment_url
        provider_status = invoice.status
        raw_payload = invoice.raw
        if invoice.amount is not None:
            amount = invoice.amount
        if invoice.currency:
            resolved_currency = invoice.currency.upper()

    record = db.create_payment(
        payment_id=payment_id,
        order_id=payment_id,
        username=normalized_username,
        chat_id=chat_id,
        plan=plan,
        amount=amount,
        currency=resolved_currency,
        provider=provider,
        provider_payment_id=provider_payment_id,
        payment_url=payment_url,
        external_status=provider_status,
        raw_provider_payload=raw_payload,
        referrer=normalized_referrer,
        source=source,
        metadata=metadata_payload,
    )

    logger.info(
        "Created payment",
        extra={
            "payment_id": payment_id,
            "username": normalized_username,
            "plan": plan,
            "amount": amount,
            "currency": resolved_currency,
            "provider": provider,
        },
    )

    return CreatePaymentResponse(
        payment_id=record["payment_id"],
        username=record["username"],
        plan=record["plan"],
        amount=record["amount"],
        currency=record.get("currency", resolved_currency),
        status=record["status"],
        provider=record.get("provider"),
        provider_payment_id=record.get("provider_payment_id"),
        payment_url=record.get("payment_url"),
    )


def _finalise_payment(
    payment: dict[str, Any],
    *,
    username: str,
    chat_id: int | None,
    plan: str,
    amount: int,
    currency: str,
    duration_days: int,
    paid_at: dt.datetime | None,
    provider_status: str | None,
    raw_provider_payload: dict[str, Any] | None,
    payment_url: str | None,
) -> ConfirmPaymentResponse:
    subscription = _ensure_subscription(username, chat_id, duration_days)
    updated = db.update_payment_status(
        payment["payment_id"],
        status="paid",
        paid_at=paid_at or dt.datetime.utcnow().replace(microsecond=0),
        key_uuid=subscription["uuid"],
        provider_status=provider_status,
        payment_url=payment_url,
        raw_provider_payload=raw_provider_payload,
    )

    if updated is None:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="payment_update_failed")

    _award_referral_bonus(username)
    resolved_currency = (updated.get("currency") or currency or MORUNE_DEFAULT_CURRENCY).upper()

    return ConfirmPaymentResponse(
        payment_id=updated["payment_id"],
        username=updated["username"],
        plan=updated["plan"],
        amount=amount,
        currency=resolved_currency,
        status=updated["status"],
        expires_at=subscription["expires_at"],
        key_uuid=subscription["uuid"],
        payment_url=updated.get("payment_url"),
    )


@router.post("/create", response_model=CreatePaymentResponse)
def create_payment(request: CreatePaymentRequest, _: None = Depends(require_service_token)):
    source = request.source or "api"
    return _create_payment_internal(
        username=request.username,
        chat_id=request.chat_id,
        plan=request.plan,
        amount_override=request.amount,
        currency=request.currency,
        referrer=request.referrer,
        metadata=request.metadata,
        source=source,
    )


@router.post("/public/create", response_model=CreatePaymentResponse)
def public_create_payment(
    request: PublicCreatePaymentRequest,
    x_form_token: str | None = Header(
        None,
        alias="X-Form-Token",
        include_in_schema=False,
    ),
):
    if PAYMENTS_PUBLIC_TOKEN:
        if x_form_token != PAYMENTS_PUBLIC_TOKEN:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid_token")
    else:
        logger.debug("PAYMENTS_PUBLIC_TOKEN is not configured; accepting public payment creation request")

    source = PAYMENTS_DEFAULT_SOURCE or "site"
    return _create_payment_internal(
        username=request.username,
        chat_id=request.chat_id,
        plan=request.plan,
        amount_override=request.amount,
        currency=request.currency,
        referrer=request.referrer,
        metadata=request.metadata,
        source=source,
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
            currency=payment.get("currency", MORUNE_DEFAULT_CURRENCY),
            status=payment["status"],
            expires_at=key["expires_at"],
            key_uuid=key["uuid"],
            payment_url=payment.get("payment_url"),
        )

    if payment["plan"] != request.plan:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="plan_mismatch")

    try:
        amount = _resolve_amount(request.plan, request.amount or payment["amount"])
        duration_days = plan_duration(request.plan)
    except KeyError:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="unknown_plan")

    paid_at = (
        dt.datetime.fromisoformat(request.paid_at)
        if request.paid_at
        else dt.datetime.utcnow().replace(microsecond=0)
    )
    currency = (request.currency or payment.get("currency") or MORUNE_DEFAULT_CURRENCY).upper()

    return _finalise_payment(
        payment,
        username=username,
        chat_id=request.chat_id if request.chat_id is not None else payment.get("chat_id"),
        plan=request.plan,
        amount=amount,
        currency=currency,
        duration_days=duration_days,
        paid_at=paid_at,
        provider_status=request.provider_status,
        raw_provider_payload=request.raw_provider_payload,
        payment_url=request.payment_url or payment.get("payment_url"),
    )


@router.post("/morune/webhook", response_model=ConfirmPaymentResponse | WebhookAck)
async def morune_webhook(request: Request):
    client = _get_morune_client()
    if client is None:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="morune_not_configured")

    body = await request.body()
    signature = (
        request.headers.get("X-Signature")
        or request.headers.get("X-Morune-Signature")
        or request.headers.get("X-Webhook-Signature")
        or request.headers.get("X-Api-Sha256-Signature")
    )

    try:
        event = client.parse_webhook(body=body, signature=signature)
    except MoruneSignatureError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid_signature")
    except MoruneAPIError:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="invalid_payload")

    payment = db.get_payment(event.payment_id)
    if payment is None:
        logger.warning("Received webhook for unknown payment", extra={"payment_id": event.payment_id})
        return WebhookAck(ok=False, payment_id=event.payment_id, detail="payment_not_found")

    status_lower = event.status.lower()
    if status_lower not in {"paid", "success", "succeeded", "completed", "done"}:
        db.update_payment_status(
            payment["payment_id"],
            status=status_lower,
            provider_status=status_lower,
            raw_provider_payload=event.raw,
        )
        return WebhookAck(ok=True, payment_id=payment["payment_id"], detail="status_recorded")

    try:
        duration_days = plan_duration(payment["plan"])
    except KeyError:
        logger.error("Webhook referenced unknown plan", extra={"plan": payment["plan"]})
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="unknown_plan")

    amount = event.amount or payment.get("amount", 0)
    currency = (event.currency or payment.get("currency") or MORUNE_DEFAULT_CURRENCY).upper()

    return _finalise_payment(
        payment,
        username=payment["username"],
        chat_id=payment.get("chat_id"),
        plan=payment["plan"],
        amount=amount,
        currency=currency,
        duration_days=duration_days,
        paid_at=event.paid_at,
        provider_status=status_lower,
        raw_provider_payload=event.raw,
        payment_url=payment.get("payment_url"),
    )


__all__ = [
    "create_payment",
    "public_create_payment",
    "confirm_payment",
    "morune_webhook",
]
