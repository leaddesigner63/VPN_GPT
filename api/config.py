from __future__ import annotations

import os
from pathlib import Path
from typing import Dict

from dotenv import load_dotenv

from api.utils.logging import get_logger

logger = get_logger("config")

_BASE_DIR = Path(__file__).resolve().parents[2]
_ENV_PATH = Path(os.getenv("ENV_PATH", _BASE_DIR / ".env"))

if _ENV_PATH.exists():
    load_dotenv(str(_ENV_PATH), override=True)
    logger.info("Loaded environment variables from file", extra={"path": str(_ENV_PATH)})
else:
    logger.warning(
        ".env file is missing; relying on existing environment variables",
        extra={"path": str(_ENV_PATH)},
    )


def require_env(name: str) -> str:
    """Return the value of an environment variable or raise an error."""

    value = os.getenv(name)
    if not value:
        logger.error("Required environment variable is missing", extra={"name": name})
        raise RuntimeError(
            "Переменная окружения {name} не задана. "
            "Создайте файл {env} или установите переменную в окружении."
            .format(name=name, env=_ENV_PATH)
        )
    logger.debug("Loaded environment variable", extra={"name": name})
    return value.strip()


def _parse_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError as exc:  # pragma: no cover - defensive
        logger.error("Failed to parse int from env", extra={"name": name, "value": raw})
        raise RuntimeError(f"Переменная окружения {name} должна быть целым числом") from exc
    return value


def _parse_plans(raw: str) -> Dict[str, int]:
    plans: Dict[str, int] = {}
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if ":" not in chunk:
            raise RuntimeError("Каждый тариф должен иметь формат <код>:<стоимость>")
        code, price = chunk.split(":", 1)
        code = code.strip()
        try:
            plans[code] = int(price.strip())
        except ValueError as exc:  # pragma: no cover - invalid configuration
            raise RuntimeError(f"Стоимость тарифа {code} должна быть целым числом") from exc
    if not plans:
        raise RuntimeError("Ни один тариф не настроен в переменной PLANS")
    return plans


VLESS_HOST = require_env("VLESS_HOST")
VLESS_PORT = _parse_int("VLESS_PORT", 2053)
BOT_PAYMENT_URL = require_env("BOT_PAYMENT_URL").rstrip("/")
TRIAL_DAYS = _parse_int("TRIAL_DAYS", 0)
PLANS = _parse_plans(os.getenv("PLANS", "1m:180,3m:460,12m:1450"))
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN")
INTERNAL_TOKEN = os.getenv("INTERNAL_TOKEN", ADMIN_TOKEN)
REFERRAL_BONUS_DAYS = _parse_int("REFERRAL_BONUS_DAYS", 30)
DEFAULT_COUNTRY = os.getenv("DEFAULT_COUNTRY", "NL")

PLAN_DURATIONS = {
    "1m": 30,
    "3m": 90,
    "12m": 365,
}

logger.info(
    "Configuration loaded",
    extra={
        "VLESS_HOST": VLESS_HOST,
        "VLESS_PORT": VLESS_PORT,
        "TRIAL_DAYS": TRIAL_DAYS,
        "PLANS": PLANS,
        "DEFAULT_COUNTRY": DEFAULT_COUNTRY,
    },
)


def plan_amount(plan_code: str) -> int:
    if plan_code not in PLANS:
        raise KeyError(plan_code)
    return PLANS[plan_code]


def plan_duration(plan_code: str) -> int:
    if plan_code in PLAN_DURATIONS:
        return PLAN_DURATIONS[plan_code]
    if plan_code.endswith("m"):
        try:
            months = int(plan_code[:-1])
        except ValueError as exc:  # pragma: no cover - configuration error
            raise RuntimeError(f"Не удалось определить длительность для тарифа {plan_code}") from exc
        return months * 30
    raise RuntimeError(f"Неизвестная длительность для тарифа {plan_code}")


__all__ = [
    "VLESS_HOST",
    "VLESS_PORT",
    "BOT_PAYMENT_URL",
    "TRIAL_DAYS",
    "PLANS",
    "plan_amount",
    "plan_duration",
    "ADMIN_TOKEN",
    "INTERNAL_TOKEN",
    "REFERRAL_BONUS_DAYS",
    "DEFAULT_COUNTRY",
]
