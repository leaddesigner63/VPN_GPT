"""FastAPI endpoints for Morune server-to-server payments."""
from __future__ import annotations

import datetime as dt
import json
import uuid
from typing import Any

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, Field

from api.config import DEFAULT_COUNTRY, MORUNE_API_KEY, MORUNE_SHOP_ID, plan_amount, plan_duration
from api.integrations.morune import MoruneAPIError, MoruneConfigurationError
from api.utils import db
from api.utils.logging import get_logger
from api.utils.morune_client import InvoiceCreateResult, create_invoice, verify_signature
from api.utils.telegram import send_message
from api.utils.vless import build_vless_link

router = APIRouter(prefix="/morune", tags=["morune"])
logger = get_logger("endpoints.morune")

_ALLOWED_PLANS = {"1m", "3m", "1y"}
_SUCCESS_STATUSES = {"paid", "success", "succeeded", "completed", "confirmed"}


class CreateInvoiceRequest(BaseModel):
    plan: str = Field(..., description="Tariff code")
    username: str | None = Field(
        None,
        description="Telegram username without @. Optional if prefilled by bot.",
        max_length=64,
    )


class CreateInvoiceResponse(BaseModel):
    ok: bool = True
    payment_url: str
    order_id: str


class WebhookResponse(BaseModel):
    ok: bool = True


def _normalise_username(raw: str | None) -> str:
    if raw is None:
        raise ValueError("username_required")
    return db.normalise_username(raw)


def _extract_username_and_plan(payload: dict[str, Any]) -> tuple[str | None, str | None]:
    custom_fields = payload.get("custom_fields") or payload.get("customFields")
    username: str | None = None
    plan: str | None = None
    if isinstance(custom_fields, str):
        try:
            parsed = json.loads(custom_fields)
        except json.JSONDecodeError:
            logger.warning("Failed to parse custom_fields JSON", extra={"order_id": payload.get("order_id")})
        else:
            if isinstance(parsed, dict):
                username = parsed.get("username") or parsed.get("user")
                plan = parsed.get("plan")
    elif isinstance(custom_fields, dict):
        username = custom_fields.get("username") or custom_fields.get("user")
        plan = custom_fields.get("plan")

    if not plan:
        meta = payload.get("metadata") or payload.get("meta") or {}
        if isinstance(meta, dict):
            plan = meta.get("plan") or meta.get("tariff")
    if not username:
        meta = payload.get("metadata") or payload.get("meta") or {}
        if isinstance(meta, dict):
            username = meta.get("username") or meta.get("user")
    return username, plan


def _extract_status(payload: dict[str, Any]) -> str:
    status_value = (
        payload.get("status")
        or payload.get("event")
        or payload.get("state")
        or payload.get("payment_status")
    )
    if isinstance(status_value, str):
        return status_value.lower()
    data = payload.get("data")
    if isinstance(data, dict):
        inner_status = (
            data.get("status")
            or data.get("state")
            or data.get("event")
            or data.get("payment_status")
        )
        if isinstance(inner_status, str):
            return inner_status.lower()
        attributes = data.get("attributes")
        if isinstance(attributes, dict):
            attr_status = attributes.get("status") or attributes.get("state")
            if isinstance(attr_status, str):
                return attr_status.lower()
    return "unknown"


def _extract_order_payload(raw: bytes) -> dict[str, Any]:
    try:
        payload = json.loads(raw.decode("utf-8"))
    except json.JSONDecodeError as exc:
        logger.warning("Morune webhook JSON decode failed")
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="invalid_json") from exc

    if isinstance(payload, dict) and "data" in payload and isinstance(payload["data"], dict):
        enriched = dict(payload)
        enriched.update(payload["data"])
        attributes = payload["data"].get("attributes")
        if isinstance(attributes, dict):
            enriched.update(attributes)
        return enriched
    if isinstance(payload, dict):
        return payload
    logger.error("Unexpected Morune webhook payload", extra={"type": type(payload).__name__})
    raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="invalid_payload")


@router.post("/create_invoice", response_model=CreateInvoiceResponse)
async def create_invoice_endpoint(request: CreateInvoiceRequest) -> CreateInvoiceResponse:
    if request.plan not in _ALLOWED_PLANS:
        logger.warning("Requested unknown plan", extra={"plan": request.plan})
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="unknown_plan")

    if not (MORUNE_API_KEY and MORUNE_SHOP_ID):
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="morune_not_configured")

    try:
        amount = plan_amount(request.plan)
    except KeyError as exc:  # pragma: no cover - configuration mismatch
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="unknown_plan") from exc

    try:
        username = _normalise_username(request.username)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc

    order_id = str(uuid.uuid4())

    record = db.create_payment(
        payment_id=order_id,
        order_id=order_id,
        username=username,
        chat_id=None,
        plan=request.plan,
        amount=amount,
        currency="RUB",
        provider="morune",
        metadata={"plan": request.plan, "username": username},
        source="site",
    )

    try:
        invoice: InvoiceCreateResult = await create_invoice(
            amount=amount,
            order_id=order_id,
            plan=request.plan,
            username=username,
            metadata=record.get("metadata"),
        )
    except MoruneConfigurationError as exc:
        db.update_payment_status(order_id, status="failed")
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="morune_not_configured") from exc
    except MoruneAPIError as exc:
        db.update_payment_status(order_id, status="failed")
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="morune_create_failed") from exc

    db.update_payment_status(
        order_id,
        status=record["status"],
        payment_url=invoice.payment_url,
        provider_status=invoice.status,
        provider_payment_id=invoice.provider_payment_id,
        raw_provider_payload=invoice.raw,
    )

    return CreateInvoiceResponse(payment_url=invoice.payment_url, order_id=order_id)


@router.post("/paid", response_model=WebhookResponse)
async def morune_paid(request: Request) -> WebhookResponse:
    signature = request.headers.get("x-api-sha256-signature")
    raw_body = await request.body()

    try:
        if not verify_signature(raw_body, signature):
            logger.warning("Morune webhook signature mismatch")
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="invalid_signature")
    except MoruneConfigurationError as exc:
        logger.error("Morune webhook secret not configured")
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="morune_not_configured") from exc

    payload = _extract_order_payload(raw_body)
    order_id = (
        payload.get("order_id")
        or payload.get("orderId")
        or payload.get("external_id")
        or payload.get("payment_id")
    )
    if not order_id:
        logger.error("Morune webhook missing order_id", extra={"payload": payload})
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="order_id_missing")

    status_value = _extract_status(payload)
    if status_value not in _SUCCESS_STATUSES:
        logger.info("Ignoring non-success Morune webhook", extra={"order_id": order_id, "status": status_value})
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="payment_not_paid")

    record = db.get_payment_by_order(str(order_id))
    if record is None:
        logger.error("Morune order not found", extra={"order_id": order_id})
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="order_not_found")

    if record.get("status") == "paid":
        logger.info("Morune webhook already processed", extra={"order_id": order_id})
        return WebhookResponse(ok=True)

    username_raw, plan_from_payload = _extract_username_and_plan(payload)
    username = record.get("username")
    if not username and username_raw:
        try:
            username = db.normalise_username(username_raw)
        except ValueError:
            username = None
    if not username:
        logger.error("Unable to determine username for Morune order", extra={"order_id": order_id})
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="username_missing")

    plan_code = record.get("plan") or plan_from_payload
    if plan_code not in _ALLOWED_PLANS:
        logger.error("Unknown plan in Morune webhook", extra={"order_id": order_id, "plan": plan_code})
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="unknown_plan")

    days = plan_duration(plan_code)
    now = dt.datetime.utcnow().replace(microsecond=0)

    extended = db.extend_active_key(username, days=days)
    if extended:
        key_uuid = extended["uuid"]
        expires_at = extended["expires_at"]
        reused = True
    else:
        expires_at_dt = (now + dt.timedelta(days=days)).replace(microsecond=0)
        key_uuid = str(uuid.uuid4())
        label = f"VPN_GPT_{username}"
        link = build_vless_link(key_uuid, label)
        created = db.create_vpn_key(
            username=username,
            chat_id=record.get("chat_id"),
            uuid_value=key_uuid,
            link=link,
            expires_at=expires_at_dt,
            label=label,
            country=DEFAULT_COUNTRY,
            trial=False,
        )
        expires_at = created["expires_at"]
        reused = False

    db.update_payment_status(
        str(order_id),
        status="paid",
        paid_at=now,
        key_uuid=key_uuid,
        provider_status=status_value,
        raw_provider_payload=payload,
    )

    user = db.get_user(username)
    chat_id = user.get("chat_id") if user else record.get("chat_id")
    if chat_id:
        message = (
            "✅ Оплата получена!\n\n"
            f"Тариф: {plan_code}.\n"
            f"VLESS UUID: {key_uuid}.\n"
            f"Доступ активен до {expires_at}."
        )
        try:
            await send_message(chat_id, message)
        except Exception:  # pragma: no cover - Telegram failure
            logger.exception("Failed to notify user about Morune payment", extra={"chat_id": chat_id})

    logger.info(
        "Processed Morune webhook",
        extra={"order_id": order_id, "username": username, "plan": plan_code, "reused": reused},
    )
    return WebhookResponse(ok=True)


legacy_router = APIRouter(prefix="/api/morune", tags=["morune"])


@legacy_router.post("/create_invoice", response_model=CreateInvoiceResponse, include_in_schema=False)
async def legacy_create_invoice_endpoint(request: CreateInvoiceRequest) -> CreateInvoiceResponse:
    """Backward-compatible path that mirrors :func:`create_invoice_endpoint`."""

    return await create_invoice_endpoint(request)


@legacy_router.post("/paid", response_model=WebhookResponse, include_in_schema=False)
async def legacy_morune_paid(request: Request) -> WebhookResponse:
    """Backward-compatible path that mirrors :func:`morune_paid`."""

    return await morune_paid(request)


__all__ = ["router", "legacy_router"]
