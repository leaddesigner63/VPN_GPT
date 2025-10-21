from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Dict, Tuple

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from api.config import STAR_SETTINGS
from api.utils.logging import get_logger
from api.utils.telegram import TelegramInvoiceError, create_invoice_link
from utils.stars import StarPlan, build_invoice_request_data

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
    payload = build_invoice_request_data(plan)
    return payload


async def _get_invoice_link(plan: StarPlan) -> str:
    key = _CacheKey(code=plan.code, price=plan.price_stars, is_subscription=plan.is_subscription)
    async with _cache_lock:
        cached = _invoice_cache.get(key)
        if cached:
            return cached

    invoice_payload = _build_invoice_payload(plan)
    try:
        link = await create_invoice_link(invoice_payload)
    except TelegramInvoiceError as exc:
        detail = exc.detail
        status = exc.status_code or 502
        if detail == "telegram_token_missing":
            status = exc.status_code or 500
        raise HTTPException(status_code=status, detail=detail) from exc

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
