from __future__ import annotations

import base64
import datetime as dt
import decimal
import importlib
import uuid


def test_prepare_metadata_serialises_complex_values(monkeypatch) -> None:
    monkeypatch.setenv("VLESS_HOST", "test.example")
    monkeypatch.setenv("VLESS_PORT", "2053")
    monkeypatch.setenv("BOT_PAYMENT_URL", "https://vpn-gpt.store")
    monkeypatch.setenv("PLANS", "1m:80")
    monkeypatch.setenv("BOT_TOKEN", "token")
    monkeypatch.setenv("GPT_API_KEY", "gpt")
    monkeypatch.setenv("ADMIN_PANEL_PASSWORD", "panelpass")

    import api.config as config_module

    importlib.reload(config_module)
    payments = importlib.reload(importlib.import_module("api.endpoints.payments"))

    payment_id = "abc123"
    now = dt.datetime(2025, 1, 1, 12, 0, 0, tzinfo=dt.UTC)
    unique = uuid.uuid4()
    raw_bytes = b"\x00\xff"
    metadata = {
        "timestamp": now,
        "identifier": unique,
        "payload": raw_bytes,
        "decimal": decimal.Decimal("42.10"),
        "nested": {"date": now.date(), "items": [1, raw_bytes, decimal.Decimal("1.5")]},
    }

    result = payments._prepare_metadata(  # type: ignore[attr-defined]
        payment_id=payment_id,
        username="alice",
        plan="1m",
        source="bot",
        chat_id=777,
        referrer="bob",
        metadata=metadata,
    )

    assert result["order_id"] == payment_id
    assert result["username"] == "alice"
    assert result["plan"] == "1m"
    assert result["source"] == "bot"
    assert result["chat_id"] == 777
    assert result["referrer"] == "bob"
    assert result["timestamp"] == now.isoformat()
    assert result["identifier"] == str(unique)
    assert result["payload"] == base64.b64encode(raw_bytes).decode("ascii")
    assert result["decimal"] == "42.10"
    assert result["nested"]["date"] == now.date().isoformat()
    assert result["nested"]["items"][1] == base64.b64encode(raw_bytes).decode("ascii")
    assert result["nested"]["items"][2] == "1.5"
