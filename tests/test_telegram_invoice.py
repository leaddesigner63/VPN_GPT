from __future__ import annotations

import asyncio
import importlib
import os
from typing import Any

import httpx
import pytest

os.environ.setdefault("BOT_TOKEN", "123456:TESTTOKEN")
os.environ.setdefault("BASE_BOT_URL", "https://api.telegram.org")

from api.utils import telegram as telegram_module


class DummyClient:
    def __init__(self, responses: list[httpx.Response], calls: list[tuple[str, dict[str, Any]]]):
        self._responses = responses
        self.calls = calls

    async def __aenter__(self) -> "DummyClient":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        return False

    async def post(self, url: str, json: dict[str, Any]) -> httpx.Response:
        self.calls.append((url, json))
        return self._responses.pop(0)


def _reload_module(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BOT_TOKEN", "123456:TESTTOKEN")
    monkeypatch.setenv("BASE_BOT_URL", "https://api.telegram.org")
    importlib.reload(telegram_module)


def test_create_invoice_link_retries_duplicate_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    _reload_module(monkeypatch)

    url = "https://api.telegram.org/bot123456:TESTTOKEN/createInvoiceLink"
    request = httpx.Request("POST", url)
    responses = [
        httpx.Response(
            400,
            json={"ok": False, "description": "Bad Request: payload is not unique"},
            request=request,
        ),
        httpx.Response(200, json={"ok": True, "result": "https://t.me/invoice"}, request=request),
    ]
    calls: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(httpx, "AsyncClient", lambda timeout=20: DummyClient(responses, calls))

    payload = {
        "title": "VPN_GPT · 1 месяц",
        "description": "Подписка VPN_GPT на 1 месяц с автопродлением",
        "currency": "XTR",
        "payload": "stars:buy:1m:initial",
        "prices": [{"label": "Ежемесячная подписка", "amount": 80}],
    }

    link = asyncio.run(telegram_module.create_invoice_link(payload))

    assert link == "https://t.me/invoice"
    assert len(calls) == 2
    assert calls[0][1]["payload"] != calls[1][1]["payload"]


def test_create_invoice_link_drops_invalid_subscription_period(monkeypatch: pytest.MonkeyPatch) -> None:
    _reload_module(monkeypatch)

    url = "https://api.telegram.org/bot123456:TESTTOKEN/createInvoiceLink"
    request = httpx.Request("POST", url)
    responses = [
        httpx.Response(
            400,
            json={"ok": False, "description": "Bad Request: subscription_period_invalid"},
            request=request,
        ),
        httpx.Response(200, json={"ok": True, "result": "https://t.me/invoice"}, request=request),
    ]
    calls: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(httpx, "AsyncClient", lambda timeout=20: DummyClient(responses, calls))

    payload = {
        "title": "VPN_GPT · 3 месяца",
        "description": "Подписка VPN_GPT на 3 месяца с автопродлением",
        "currency": "XTR",
        "payload": "stars:buy:3m:initial",
        "prices": [{"label": "Подписка на 3 месяца", "amount": 200}],
        "subscription_period": 3600,
    }

    link = asyncio.run(telegram_module.create_invoice_link(payload))

    assert link == "https://t.me/invoice"
    assert len(calls) == 2
    assert "subscription_period" in calls[0][1]
    assert "subscription_period" not in calls[1][1]
