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
    "test_1d": "Тест на 24 часа",
    "1m": "1 месяц",
    "3m": "3 месяца",
    "6m": "6 месяцев",
    "1y": "12 месяцев",
    "12m": "12 месяцев",
    "sub_1m": "Подписка на 1 месяц",
}

_PLAN_LABELS = {
    "test_1d": "Тест 24 часа",
    "1m": "Подписка на 1 месяц",
    "3m": "Подписка на 3 месяца",
    "6m": "Подписка на 6 месяцев",
    "1y": "Подписка на 12 месяцев",
    "12m": "Подписка на 12 месяцев",
    "sub_1m": "Ежемесячная подписка",
}

_PLAN_DURATIONS = {
    "test_1d": 1,
    "1m": 30,
    "3m": 90,
    "6m": 180,
    "1y": 365,
    "12m": 365,
    "sub_1m": 30,
}

_SECONDS_IN_DAY = 24 * 60 * 60


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
    subscription_period = duration * _SECONDS_IN_DAY if subscription else None
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
    price_test = _parse_int("STARS_PRICE_TEST", 20)
    price_month = _parse_int("STARS_PRICE_MONTH", 80)

    legacy_6m = os.getenv("STARS_PRICE_6M")
    explicit_3m = os.getenv("STARS_PRICE_3M")
    if explicit_3m is None and legacy_6m is not None:
        price_3m = _parse_int("STARS_PRICE_6M", 200)
        price_6m = 0
    else:
        price_3m = _parse_int("STARS_PRICE_3M", 200)
        price_6m = _parse_int("STARS_PRICE_6M", 0)

    price_year = _parse_int("STARS_PRICE_YEAR", 700)
    # Все основные тарифы теперь оформляются по подписке, поэтому принудительно
    # включаем режим подписок, даже если переменная окружения явно отключает его.
    raw_subscription_env = os.getenv("STARS_SUBSCRIPTION_ENABLED")
    subscription_enabled = True
    if raw_subscription_env:
        subscription_enabled = _parse_bool(raw_subscription_env, True)
    if not subscription_enabled:
        subscription_enabled = True

    plans: Dict[str, StarPlan] = {}
    test_plan = _build_plan("test_1d", price_test)
    if test_plan:
        plans[test_plan.code] = test_plan
    month_plan = _build_plan("1m", price_month, subscription=True)
    if month_plan:
        plans[month_plan.code] = month_plan
    plan_3m = _build_plan("3m", price_3m, subscription=True)
    if plan_3m:
        plans[plan_3m.code] = plan_3m
    plan_6m = _build_plan("6m", price_6m, subscription=True)
    if plan_6m:
        plans[plan_6m.code] = plan_6m
    year_plan = _build_plan("1y", price_year, subscription=True)
    if year_plan:
        plans[year_plan.code] = year_plan

    return StarSettings(
        enabled=enabled and bool(plans),
        subscription_enabled=subscription_enabled,
        plans=plans,
        subscription_plan=None,
    )


def build_invoice_payload(plan_code: str) -> str:
    return f"stars:buy:{plan_code}"
