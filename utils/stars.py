from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Dict, Iterable, Optional

__all__ = [
    "StarPlan",
    "StarSettings",
    "load_star_settings",
    "resolve_plan_duration",
    "build_invoice_payload",
]

_ALLOWED_BOOL_TRUE = {"1", "true", "yes", "y", "on", "enable", "enabled"}
_ALLOWED_BOOL_FALSE = {"0", "false", "no", "n", "off", "disable", "disabled"}


def _strip_inline_comment(raw: str) -> str:
    comment_pos = raw.find("#")
    if comment_pos == -1:
        return raw.strip()
    return raw[:comment_pos].strip()


def _parse_bool(raw: Optional[str], default: bool) -> bool:
    if raw is None:
        return default
    cleaned = _strip_inline_comment(raw)
    if not cleaned:
        return default
    lowered = cleaned.lower()
    if lowered in _ALLOWED_BOOL_TRUE:
        return True
    if lowered in _ALLOWED_BOOL_FALSE:
        return False
    return default


def _parse_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    cleaned = _strip_inline_comment(raw)
    if cleaned == "":
        return default
    try:
        return int(cleaned)
    except ValueError as exc:  # pragma: no cover - defensive
        raise RuntimeError(f"Переменная окружения {name} должна быть целым числом") from exc


_PLAN_TITLES = {
    "1m": "1 месяц",
    "3m": "3 месяца",
    "1y": "12 месяцев",
    "12m": "12 месяцев",
    "sub_1m": "Подписка на 1 месяц",
}

_PLAN_LABELS = {
    "1m": "1 месяц",
    "3m": "3 месяца",
    "1y": "12 месяцев",
    "12m": "12 месяцев",
    "sub_1m": "Ежемесячная подписка",
}

_PLAN_DURATIONS = {
    "1m": 30,
    "3m": 90,
    "1y": 365,
    "12m": 365,
    "sub_1m": 30,
}

_SUBSCRIPTION_PERIOD_SECONDS = 30 * 24 * 60 * 60


@dataclass(frozen=True)
class StarPlan:
    code: str
    price_stars: int
    title: str
    label: str
    duration_days: int
    is_subscription: bool = False
    subscription_period: int | None = None

    @property
    def payload(self) -> str:
        return build_invoice_payload(self.code)


@dataclass(frozen=True)
class StarSettings:
    enabled: bool
    subscription_enabled: bool
    plans: Dict[str, StarPlan]
    subscription_plan: StarPlan | None = None

    def available_plans(self) -> Iterable[StarPlan]:
        return self.plans.values()


def resolve_plan_duration(plan_code: str) -> int:
    try:
        return _PLAN_DURATIONS[plan_code]
    except KeyError as exc:  # pragma: no cover - defensive
        raise RuntimeError(f"Неизвестный тариф Stars: {plan_code}") from exc


def _build_plan(code: str, price: int, *, subscription: bool = False) -> StarPlan | None:
    if price <= 0:
        return None
    duration = resolve_plan_duration(code)
    title = _PLAN_TITLES.get(code, code)
    label = _PLAN_LABELS.get(code, title)
    subscription_period = _SUBSCRIPTION_PERIOD_SECONDS if subscription else None
    return StarPlan(
        code=code,
        price_stars=price,
        title=title,
        label=label,
        duration_days=duration,
        is_subscription=subscription,
        subscription_period=subscription_period,
    )


def load_star_settings() -> StarSettings:
    enabled = _parse_bool(os.getenv("STARS_ENABLED"), True)
    price_month = _parse_int("STARS_PRICE_MONTH", 300)
    price_3m = _parse_int("STARS_PRICE_3M", 800)
    price_year = _parse_int("STARS_PRICE_YEAR", 2400)
    subscription_enabled = _parse_bool(os.getenv("STARS_SUBSCRIPTION_ENABLED"), False)

    plans: Dict[str, StarPlan] = {}
    month_plan = _build_plan("1m", price_month)
    if month_plan:
        plans[month_plan.code] = month_plan
    plan_3m = _build_plan("3m", price_3m)
    if plan_3m:
        plans[plan_3m.code] = plan_3m
    year_plan = _build_plan("1y", price_year)
    if year_plan:
        plans[year_plan.code] = year_plan

    subscription_plan = None
    if subscription_enabled and month_plan:
        subscription_plan = _build_plan("sub_1m", price_month, subscription=True)

    return StarSettings(
        enabled=enabled and bool(plans),
        subscription_enabled=subscription_enabled and subscription_plan is not None,
        plans=plans,
        subscription_plan=subscription_plan,
    )


def build_invoice_payload(plan_code: str) -> str:
    return f"stars:buy:{plan_code}"
