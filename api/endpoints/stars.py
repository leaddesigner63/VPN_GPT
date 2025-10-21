from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Dict, Tuple

import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from api.config import STAR_SETTINGS
from api.utils.logging import get_logger
from api.utils.telegram import BASE as TELEGRAM_BASE, BOT_TOKEN
from utils.stars import StarPlan, build_invoice_payload

logger = get_logger("endpoints.stars")
router = APIRouter(prefix="/stars", tags=["stars"])


@dataclass(slots=True, frozen=True)
class _CacheKey:
    code: str
    price: int
    is_subscription: bool


class StarInvoice(BaseModel):
    plan: str
    title: str
    price_stars: int
    link: str
    duration_days: int
    is_subscription: bool = False


class StarInvoicesResponse(BaseModel):
    ok: bool = True
    enabled: bool
    plans: Dict[str, StarInvoice]


_cache_lock = asyncio.Lock()
_invoice_cache: dict[_CacheKey, str] = {}


def _build_invoice_payload(plan: StarPlan) -> dict[str, Any]:
    payload = {
        "title": f"VPN_GPT · {plan.title}",
        "description": f"Доступ к VPN_GPT на {plan.title.lower()}",
        "currency": "XTR",
        "payload": build_invoice_payload(plan.code),
        "prices": [
            {
                "label": plan.label,
                "amount": plan.price_stars,
            }
        ],
    }
    if plan.subscription_period:
        payload["subscription_period"] = plan.subscription_period
    return payload


async def _request_telegram_invoice(data: dict[str, Any]) -> str:
    if not BOT_TOKEN:
        logger.error("Cannot create invoice link: BOT_TOKEN is not configured")
        raise HTTPException(status_code=500, detail="telegram_token_missing")

    url = f"{TELEGRAM_BASE}/bot{BOT_TOKEN}/createInvoiceLink"
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.post(url, json=data)
    except httpx.RequestError as exc:
        logger.exception("Failed to call Telegram createInvoiceLink", extra={"error": str(exc)})
        raise HTTPException(status_code=502, detail="telegram_request_failed") from exc

    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:  # pragma: no cover - defensive
        logger.exception(
            "Telegram returned HTTP error for createInvoiceLink",
            extra={"status": exc.response.status_code, "body": exc.response.text},
        )
        raise HTTPException(status_code=502, detail="telegram_http_error") from exc

    payload = response.json()
    if not payload.get("ok"):
        logger.error(
            "Telegram createInvoiceLink returned failure",
            extra={"response": payload},
        )
        raise HTTPException(status_code=502, detail="telegram_error")

    result = payload.get("result")
    if not isinstance(result, str):
        logger.error("Unexpected Telegram response payload", extra={"result": result})
        raise HTTPException(status_code=502, detail="telegram_invalid_response")

    return result


async def _get_invoice_link(plan: StarPlan) -> str:
    key = _CacheKey(code=plan.code, price=plan.price_stars, is_subscription=plan.is_subscription)
    async with _cache_lock:
        cached = _invoice_cache.get(key)
        if cached:
            return cached

    invoice_payload = _build_invoice_payload(plan)
    link = await _request_telegram_invoice(invoice_payload)

    async with _cache_lock:
        _invoice_cache[key] = link

    logger.info(
        "Created Telegram Stars invoice link",
        extra={"plan": plan.code, "subscription": plan.is_subscription},
    )
    return link


@router.get("/invoices", response_model=StarInvoicesResponse)
async def get_star_invoices() -> StarInvoicesResponse:
    if not STAR_SETTINGS.enabled:
        return StarInvoicesResponse(ok=True, enabled=False, plans={})

    plans: Dict[str, StarInvoice] = {}
    for plan in STAR_SETTINGS.available_plans():
        link = await _get_invoice_link(plan)
        plans[plan.code] = StarInvoice(
            plan=plan.code,
            title=plan.title,
            price_stars=plan.price_stars,
            link=link,
            duration_days=plan.duration_days,
            is_subscription=plan.is_subscription,
        )

    return StarInvoicesResponse(
        ok=True,
        enabled=True,
        plans=plans,
    )
