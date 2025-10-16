import asyncio

import pytest

from utils.content_filters import assert_no_geoblocking, contains_geoblocking, sanitize_text


def test_sanitize_text_replaces_russian_phrase():
    original = "Гео-блокировки больше не проблема"
    sanitized = sanitize_text(original)
    assert sanitized == "[скрыто] больше не проблема"
    assert not contains_geoblocking(sanitized)


def test_sanitize_text_replaces_english_phrase():
    original = "Avoid GEO blocking on any service"
    sanitized = sanitize_text(original)
    assert sanitized == "Avoid [скрыто] on any service"
    assert not contains_geoblocking(sanitized)


def test_contains_geoblocking_detects_spaced_variant():
    assert contains_geoblocking("гео блокировка работает")


def test_assert_no_geoblocking_raises():
    with pytest.raises(ValueError):
        assert_no_geoblocking("geo-blocking is active")


def test_send_message_applies_sanitizer(monkeypatch):
    from api.utils import telegram

    captured: dict[str, dict] = {}

    class DummyResponse:
        status_code = 200

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {"ok": True}

    class DummyClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def post(self, url, json):
            captured["payload"] = json
            return DummyResponse()

    monkeypatch.setattr(telegram, "BOT_TOKEN", "token")
    monkeypatch.setattr(telegram, "BASE", "https://example.com")
    monkeypatch.setattr(telegram.httpx, "AsyncClient", lambda timeout=30: DummyClient())

    asyncio.run(telegram.send_message(123, "гео блокировка снята"))

    assert captured["payload"]["text"] == "[скрыто] снята"
