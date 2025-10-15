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


async def get_default_service(
    *, client: httpx.AsyncClient | None = None
) -> str:
    """Return the first enabled payment tariff for the configured shop."""

    if not (MORUNE_API_KEY and MORUNE_SHOP_ID):
        logger.error("Morune configuration is incomplete")
        raise legacy_morune.MoruneConfigurationError("morune_not_configured")

    headers = {
        "x-api-key": MORUNE_API_KEY,
        "accept": "application/json",
    }
    if MORUNE_SHOP_ID:
        headers.setdefault("x-shop-id", MORUNE_SHOP_ID)
        headers.setdefault("x-project-id", MORUNE_SHOP_ID)

    own_client = False
    if client is None:
        client = httpx.AsyncClient(timeout=httpx.Timeout(15.0, connect=5.0), trust_env=False)
        own_client = True

    try:
        try:
            response = await client.get(
                f"{MORUNE_BASE_URL}/shops/{MORUNE_SHOP_ID}/payment-tariffs",
                headers=headers,
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:  # pragma: no cover - network failure
            logger.exception(
                "Morune payment tariffs request failed",
                extra={"shop_id": MORUNE_SHOP_ID, "error": str(exc)},
            )
            raise legacy_morune.MoruneAPIError("morune_request_failed") from exc

        try:
            payload = response.json()
        except ValueError as exc:  # pragma: no cover - invalid JSON
            logger.exception(
                "Morune returned invalid tariffs JSON",
                extra={"shop_id": MORUNE_SHOP_ID},
            )
            raise legacy_morune.MoruneAPIError("morune_invalid_json") from exc
    finally:
        if own_client:
            await client.aclose()

    def _iter_tariffs(data: Any) -> list[dict[str, Any]]:
        if isinstance(data, list):
            return [item for item in data if isinstance(item, dict)]
        if isinstance(data, dict):
            for key in ("data", "items", "results", "tariffs"):
                nested = data.get(key)
                if isinstance(nested, list):
                    return [item for item in nested if isinstance(item, dict)]
            attributes = data.get("attributes")
            if isinstance(attributes, list):
                return [item for item in attributes if isinstance(item, dict)]
        return []

    tariffs = _iter_tariffs(payload)
    if not tariffs and isinstance(payload, dict):
        attributes = payload.get("data")
        if isinstance(attributes, dict):
            tariffs = _iter_tariffs(attributes)

    for tariff in tariffs:
        status_value: Any = tariff.get("status")
        if status_value is None and isinstance(tariff.get("attributes"), dict):
            status_value = tariff["attributes"].get("status")
        if isinstance(status_value, str):
            status_normalized = status_value.lower()
            is_enabled = status_normalized == "enabled"
        elif isinstance(status_value, bool):
            is_enabled = status_value
        else:
            is_enabled = False
        if not is_enabled:
            continue

        include_value: Any = tariff.get("include_service")
        if include_value is None and isinstance(tariff.get("attributes"), dict):
            include_value = tariff["attributes"].get("include_service")
        if include_value is None:
            include_value = tariff.get("service") or tariff.get("code") or tariff.get("id")

        if isinstance(include_value, list):
            for item in include_value:
                if item:
                    return str(item)
            continue
        if include_value:
            return str(include_value)

    logger.error("No enabled Morune payment tariffs found", extra={"shop_id": MORUNE_SHOP_ID})
    raise legacy_morune.MoruneAPIError("morune_tariff_not_found")


async def create_invoice(
    amount: int,
    order_id: str,
    plan: str,
    username: str | None,
    *,
    metadata: dict[str, Any] | None = None,
) -> InvoiceCreateResult:
    """Create a Morune invoice and extract the payment URL."""

    if not (MORUNE_API_KEY and MORUNE_SHOP_ID):
        logger.error("Morune configuration is incomplete")
        raise legacy_morune.MoruneConfigurationError("morune_not_configured")

    currency_code = (MORUNE_DEFAULT_CURRENCY or "RUB").upper()
    metadata_payload = dict(metadata or {})

    custom_fields: dict[str, Any] = {"plan": plan}
    if username:
        custom_fields["username"] = username

    headers = {
        "x-api-key": MORUNE_API_KEY,
        "accept": "application/json",
        "content-type": "application/json",
    }
    if MORUNE_SHOP_ID:
        headers.setdefault("x-shop-id", MORUNE_SHOP_ID)
        headers.setdefault("x-project-id", MORUNE_SHOP_ID)

    payload: dict[str, Any] = {
        "amount": int(amount),
        "order_id": order_id,
        "currency": currency_code,
        "comment": f"VPN_GPT {plan}",
        "success_url": MORUNE_SUCCESS_URL,
        "fail_url": MORUNE_FAIL_URL,
        "hook_url": MORUNE_HOOK_URL,
        "shop_id": MORUNE_SHOP_ID,
        "expire": 300,
        "custom_fields": json.dumps(custom_fields, ensure_ascii=False, separators=(",", ":")),
    }
    url = f"{MORUNE_BASE_URL}/invoice/create"
    logger.info(
        "Creating Morune invoice",
        extra={"order_id": order_id, "plan": plan, "amount": amount},
    )
    if metadata_payload:
        logger.debug(
            "Morune invoice metadata provided",
            extra={"order_id": order_id, "keys": list(metadata_payload.keys())},
        )

    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(15.0, connect=5.0), trust_env=False
        ) as client:
            include_service = await get_default_service(client=client)
            payload["include_service"] = [include_service]
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
    """Validate Morune webhook HMAC signature using sorted JSON payload."""

    if not MORUNE_WEBHOOK_SECRET:
        raise legacy_morune.MoruneConfigurationError("morune_webhook_secret_missing")
    if not header_sig:
        return False

    try:
        parsed = json.loads(raw_body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        logger.warning("Failed to decode Morune webhook payload for signature validation")
        return False

    sorted_json = json.dumps(
        parsed,
        ensure_ascii=False,
        separators=(",", ": "),
        sort_keys=True,
    )
    digest = hmac.new(
        MORUNE_WEBHOOK_SECRET.encode("utf-8"),
        sorted_json.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(digest, header_sig)


__all__ = [
    "InvoiceCreateResult",
    "create_invoice",
    "get_default_service",
    "verify_signature",
]
