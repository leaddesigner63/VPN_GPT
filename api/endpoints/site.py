"""Site-facing endpoints for exposing environment-driven metadata."""
from __future__ import annotations

from typing import Iterable, List

from fastapi import APIRouter
from pydantic import BaseModel, Field

from api.config import STAR_SETTINGS
from api.utils.logging import get_logger

logger = get_logger("endpoints.site")

router = APIRouter(prefix="/api/site", tags=["site"])


class SitePlan(BaseModel):
    """Public representation of a plan used on the marketing website."""

    code: str = Field(..., description="Код тарифа (например, 1m или 1y)")
    title: str = Field(..., description="Человекочитаемое название тарифа")
    price_stars: int = Field(..., description="Стоимость тарифа в звёздах Telegram")
    duration_days: int = Field(..., description="Продолжительность тарифа в днях")


class SitePricingResponse(BaseModel):
    """Payload returned for the landing page pricing widget."""

    ok: bool = Field(True, description="Флаг успешного ответа")
    plans: List[SitePlan] = Field(default_factory=list, description="Доступные планы")
    test: SitePlan | None = Field(
        None, description="Информация о тестовом тарифе, если он включён"
    )


def _sort_plans(plans: Iterable[SitePlan]) -> list[SitePlan]:
    """Sort plans by duration and then by price to keep UI stable."""

    return sorted(plans, key=lambda item: (item.duration_days, item.price_stars, item.code))


@router.get("/pricing", response_model=SitePricingResponse)
def get_site_pricing() -> SitePricingResponse:
    """Expose Telegram Stars pricing configured via the environment."""

    if not STAR_SETTINGS.enabled:
        logger.info("Stars payments disabled; returning empty pricing payload")
        return SitePricingResponse(ok=True, plans=[], test=None)

    plans: list[SitePlan] = []
    test_plan: SitePlan | None = None

    for plan in STAR_SETTINGS.available_plans():
        site_plan = SitePlan(
            code=plan.code,
            title=plan.title,
            price_stars=plan.price_stars,
            duration_days=plan.duration_days,
        )
        if plan.code == "test_1d":
            test_plan = site_plan
        else:
            plans.append(site_plan)

    sorted_plans = _sort_plans(plans)
    logger.debug(
        "Returning pricing payload",
        extra={
            "plan_codes": [plan.code for plan in sorted_plans],
            "test_plan": test_plan.code if test_plan else None,
        },
    )
    return SitePricingResponse(ok=True, plans=sorted_plans, test=test_plan)
