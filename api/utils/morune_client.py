"""Helpers for interacting with the Morune payment API."""
from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass
from typing import Any

import httpx

from api.config import (
    MORUNE_API_KEY,
    MORUNE_BASE_URL,
    MORUNE_DEFAULT_CURRENCY,
    MORUNE_FAIL_URL,
    MORUNE_HOOK_URL,
    MORUNE_SHOP_ID,
    MORUNE_SUCCESS_URL,
    MORUNE_WEBHOOK_SECRET,
)
from api.integrations import morune as legacy_morune
from api.utils.logging import get_logger

logger = get_logger("utils.morune_client")


@dataclass(slots=True)
class InvoiceCreateResult:
    payment_url: str
    order_id: str
    provider_payment_id: str | None
    status: str | None
    amount: int | None
    currency: str | None
    raw: dict[str, Any]


async def create_invoice(amount: int, order_id: str, plan: str, username: str | None) -> InvoiceCreateResult:
    """Create a Morune invoice and extract the payment URL."""

    if not (MORUNE_API_KEY and MORUNE_SHOP_ID):
        logger.error("Morune configuration is incomplete")
        raise legacy_morune.MoruneConfigurationError("morune_not_configured")

    payload: dict[str, Any] = {
        "amount": int(amount),
        "order_id": order_id,
        "currency": MORUNE_DEFAULT_CURRENCY or "RUB",
        "comment": f"VPN_GPT {plan}",
        "success_url": MORUNE_SUCCESS_URL,
        "fail_url": MORUNE_FAIL_URL,
        "hook_url": MORUNE_HOOK_URL,
        "shop_id": MORUNE_SHOP_ID,
        "include_service": ["card"],
    }

    custom_fields: dict[str, Any] = {"plan": plan}
    if username:
        custom_fields["username"] = username
    payload["custom_fields"] = json.dumps(custom_fields, ensure_ascii=False)

    url = f"{MORUNE_BASE_URL}/invoice/create"
    headers = {
        "x-api-key": MORUNE_API_KEY,
        "accept": "application/json",
        "content-type": "application/json",
    }

    logger.info(
        "Creating Morune invoice",
        extra={"order_id": order_id, "plan": plan, "amount": amount},
    )

    async with httpx.AsyncClient(timeout=httpx.Timeout(15.0, connect=5.0)) as client:
        try:
            response = await client.post(url, headers=headers, json=payload)
            response.raise_for_status()
        except httpx.HTTPError as exc:  # pragma: no cover - network failure
            logger.exception(
                "Morune invoice request failed",
                extra={"order_id": order_id, "status_code": getattr(exc.response, "status_code", None)},
            )
            raise legacy_morune.MoruneAPIError("morune_request_failed") from exc

    try:
        data = response.json()
    except ValueError as exc:  # pragma: no cover - invalid JSON
        logger.exception("Morune returned invalid JSON", extra={"order_id": order_id})
        raise legacy_morune.MoruneAPIError("morune_invalid_json") from exc

    parser = legacy_morune.MoruneClient(
        base_url=MORUNE_BASE_URL,
        api_key=MORUNE_API_KEY,
        shop_id=MORUNE_SHOP_ID,
        webhook_secret=MORUNE_WEBHOOK_SECRET,
    )
    try:
        extracted = parser._extract_invoice_fields(  # type: ignore[attr-defined]
            payload=data,
            payment_id=order_id,
            fallback_currency=MORUNE_DEFAULT_CURRENCY,
        )
    finally:
        parser.close()

    payment_url = extracted.get("payment_url")
    if not payment_url:
        logger.error("Payment URL missing in Morune response", extra={"order_id": order_id})
        raise legacy_morune.MoruneAPIError("morune_payment_url_missing")

    logger.info(
        "Morune invoice created",
        extra={
            "order_id": order_id,
            "plan": plan,
            "status": extracted.get("status"),
            "provider_payment_id": extracted.get("provider_payment_id"),
        },
    )

    return InvoiceCreateResult(
        payment_url=payment_url,
        order_id=order_id,
        provider_payment_id=extracted.get("provider_payment_id"),
        status=extracted.get("status"),
        amount=extracted.get("amount"),
        currency=extracted.get("currency"),
        raw=data,
    )


def verify_signature(raw_body: bytes, header_sig: str | None) -> bool:
    """Validate Morune webhook HMAC signature."""

    if not MORUNE_WEBHOOK_SECRET:
        raise legacy_morune.MoruneConfigurationError("morune_webhook_secret_missing")
    if not header_sig:
        return False
    digest = hmac.new(MORUNE_WEBHOOK_SECRET.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(digest, header_sig)


__all__ = ["InvoiceCreateResult", "create_invoice", "verify_signature"]
