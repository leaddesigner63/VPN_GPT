from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import importlib

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


@dataclass
class MoruneTestEnv:
    client: TestClient
    database: Path


@pytest.fixture()
def morune_app(tmp_path, monkeypatch) -> MoruneTestEnv:
    db_path = tmp_path / "morune.db"
    env_path = tmp_path / ".env"
    env_path.write_text(
        "\n".join(
            [
                "VLESS_HOST=test.example",
                "VLESS_PORT=2053",
                "BOT_PAYMENT_URL=https://vpn-gpt.store",
                "PLANS=1m:180,3m:460,1y:1450",
                "MORUNE_API_KEY=test-api",
                "MORUNE_SHOP_ID=shop-123",
                "MORUNE_WEBHOOK_SECRET=hook-secret",
            ]
        ),
        encoding="utf-8",
    )

    monkeypatch.setenv("ENV_PATH", str(env_path))
    monkeypatch.setenv("DATABASE", str(db_path))
    monkeypatch.setenv("BOT_TOKEN", "bot-token")
    monkeypatch.setenv("GPT_API_KEY", "gpt-token")
    monkeypatch.setenv("MORUNE_API_KEY", "test-api")
    monkeypatch.setenv("MORUNE_SHOP_ID", "shop-123")
    monkeypatch.setenv("MORUNE_WEBHOOK_SECRET", "hook-secret")

    import api.config as config_module
    import api.utils.db as db_module
    import api.utils.morune_client as morune_client_module
    import api.main as api_main

    for module in (config_module, db_module, morune_client_module, api_main):
        importlib.reload(module)

    db_module.init_db()

    app = FastAPI(root_path="/api")
    app.include_router(api_main.morune.router)
    app.include_router(api_main.morune.legacy_router)

    client = TestClient(app)
    try:
        yield MoruneTestEnv(client=client, database=db_path)
    finally:
        client.close()


def _read_table(db_path: Path, table: str) -> list[dict[str, Any]]:
    import sqlite3

    con = sqlite3.connect(db_path)
    try:
        con.row_factory = sqlite3.Row
        cur = con.execute(f"SELECT * FROM {table}")
        return [dict(row) for row in cur.fetchall()]
    finally:
        con.close()


def _sign(payload: dict[str, Any]) -> tuple[str, bytes]:
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ": "),
        sort_keys=True,
    ).encode("utf-8")
    digest = hmac.new(b"hook-secret", canonical, hashlib.sha256).hexdigest()
    return digest, canonical


def test_create_invoice_endpoint_returns_url(monkeypatch, morune_app: MoruneTestEnv) -> None:
    from api.endpoints import morune as morune_endpoint
    from api.utils.morune_client import InvoiceCreateResult

    async def fake_create_invoice(
        *,
        amount: int,
        order_id: str,
        plan: str,
        username: str | None,
        metadata: dict[str, Any] | None = None,
    ) -> InvoiceCreateResult:
        return InvoiceCreateResult(
            payment_url="https://pay.example/checkout",
            order_id=order_id,
            provider_payment_id="inv-001",
            status="pending",
            amount=amount,
            currency="RUB",
            raw={"ok": True},
        )

    monkeypatch.setattr(morune_endpoint, "create_invoice", fake_create_invoice)

    response = morune_app.client.post(
        "/api/morune/create_invoice",
        json={"plan": "1m", "username": "alice"},
    )

    assert response.status_code == 200
    data = response.json()
    assert data["ok"] is True
    assert data["payment_url"] == "https://pay.example/checkout"
    assert len(data["order_id"]) == 36

    payments = _read_table(morune_app.database, "payments")
    assert len(payments) == 1
    assert payments[0]["order_id"] == data["order_id"]
    assert payments[0]["status"] == "pending"
    assert payments[0]["provider"] == "morune"


def test_morune_webhook_issues_key(monkeypatch, morune_app: MoruneTestEnv) -> None:
    from api.endpoints import morune as morune_endpoint
    from api.utils.morune_client import InvoiceCreateResult

    notifications: list[tuple[Any, Any]] = []

    from api.utils import db as db_module

    db_module.upsert_user("bob", 12345)

    async def fake_create_invoice(
        *,
        amount: int,
        order_id: str,
        plan: str,
        username: str | None,
        metadata: dict[str, Any] | None = None,
    ) -> InvoiceCreateResult:
        return InvoiceCreateResult(
            payment_url="https://pay.example/checkout",
            order_id=order_id,
            provider_payment_id="inv-002",
            status="pending",
            amount=amount,
            currency="RUB",
            raw={"ok": True},
        )

    async def fake_send_message(chat_id: int, text: str, parse_mode: str | None = None) -> None:  # noqa: ARG001
        notifications.append((chat_id, text))

    monkeypatch.setattr(morune_endpoint, "create_invoice", fake_create_invoice)
    monkeypatch.setattr(morune_endpoint, "send_message", fake_send_message)

    create_response = morune_app.client.post(
        "/api/morune/create_invoice",
        json={"plan": "1m", "username": "bob"},
    )
    order_id = create_response.json()["order_id"]

    payload = {
        "data": {
            "order_id": order_id,
            "status": "paid",
            "custom_fields": json.dumps({"username": "bob", "plan": "1m"}),
        }
    }
    signature, raw = _sign(payload)

    webhook = morune_app.client.post(
        "/api/morune/paid",
        data=raw,
        headers={"x-api-sha256-signature": signature, "content-type": "application/json"},
    )
    assert webhook.status_code == 200
    assert webhook.json() == {"ok": True}

    payments = _read_table(morune_app.database, "payments")
    assert payments[0]["status"] == "paid"
    assert payments[0]["key_uuid"]

    keys = _read_table(morune_app.database, "vpn_keys")
    assert len(keys) == 1
    first_key_uuid = keys[0]["uuid"]

    repeat = morune_app.client.post(
        "/api/morune/paid",
        data=raw,
        headers={"x-api-sha256-signature": signature, "content-type": "application/json"},
    )
    assert repeat.status_code == 200
    assert repeat.json() == {"ok": True}

    keys_after = _read_table(morune_app.database, "vpn_keys")
    assert len(keys_after) == 1
    assert keys_after[0]["uuid"] == first_key_uuid

    assert notifications, "Telegram notification was not sent"
    notified_chat, message = notifications[0]
    assert notified_chat == 12345
    assert isinstance(message, str) and "Оплата получена" in message


def test_morune_webhook_rejects_invalid_signature(morune_app: MoruneTestEnv) -> None:
    payload = {"data": {"order_id": "missing", "status": "paid"}}
    raw = json.dumps(payload).encode("utf-8")
    response = morune_app.client.post(
        "/api/morune/paid",
        data=raw,
        headers={"x-api-sha256-signature": "invalid", "content-type": "application/json"},
    )
    assert response.status_code == 400
    body = response.json()
    assert body["detail"] == "invalid_signature"
