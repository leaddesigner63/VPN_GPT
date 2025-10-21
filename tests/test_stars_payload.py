from __future__ import annotations

from utils.stars import (
    STAR_PAYLOAD_PREFIX,
    build_invoice_payload,
    extract_plan_code_from_payload,
    load_star_settings,
)


def test_build_invoice_payload_generates_unique_suffixes() -> None:
    payload_1 = build_invoice_payload("1m")
    payload_2 = build_invoice_payload("1m")

    assert payload_1 != payload_2

    plan_1, suffix_1 = extract_plan_code_from_payload(payload_1)
    plan_2, suffix_2 = extract_plan_code_from_payload(payload_2)

    assert plan_1 == "1m"
    assert plan_2 == "1m"
    assert suffix_1 is not None
    assert suffix_2 is not None
    assert suffix_1 != suffix_2


def test_extract_plan_code_handles_legacy_payloads_without_suffix() -> None:
    plan, suffix = extract_plan_code_from_payload(f"{STAR_PAYLOAD_PREFIX}test_1d")

    assert plan == "test_1d"
    assert suffix is None


def test_extract_plan_code_returns_none_for_invalid_payload() -> None:
    plan, suffix = extract_plan_code_from_payload("invalid")

    assert plan is None
    assert suffix is None


def test_yearly_subscription_period_is_supported(monkeypatch) -> None:
    monkeypatch.setenv("STARS_PRICE_TEST", "20")
    monkeypatch.setenv("STARS_PRICE_MONTH", "80")
    monkeypatch.setenv("STARS_PRICE_3M", "200")
    monkeypatch.setenv("STARS_PRICE_6M", "0")
    monkeypatch.setenv("STARS_PRICE_YEAR", "700")
    monkeypatch.setenv("STARS_ENABLED", "true")
    monkeypatch.setenv("STARS_SUBSCRIPTION_ENABLED", "true")

    settings = load_star_settings()

    yearly = settings.plans["1y"]

    assert yearly.duration_days == 365
    # Telegram принимает только кратные 30 дням значения для автопродления.
    assert yearly.subscription_period == 360 * 24 * 60 * 60
