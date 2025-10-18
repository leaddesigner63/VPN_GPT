from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from tests.test_vpn_api import auth_headers


pytest_plugins = ("tests.test_vpn_api",)


@pytest.mark.usefixtures("configured_env")
def test_users_summary_returns_combined_stats(api_app, monkeypatch):
    from api.utils import db as db_module

    monkeypatch.setattr(db_module.xray, "add_client_no_duplicates", lambda uuid, label: True)

    db_module.upsert_user("alice", 123, referrer="ref")
    expires_at = datetime.now(UTC) + timedelta(days=30)
    db_module.create_vpn_key(
        username="alice",
        chat_id=123,
        uuid_value="uuid-alice",
        link="vpn://alice",
        label="alice",
        expires_at=expires_at,
        trial=True,
    )
    paid_at = datetime.now(UTC)
    db_module.create_payment(
        payment_id="pay-alice",
        order_id="order-alice",
        username="alice",
        chat_id=123,
        plan="1m",
        amount=250,
        currency="RUB",
        status="paid",
    )
    db_module.update_payment_status("pay-alice", status="paid", paid_at=paid_at)

    response = api_app.get("/users/summary", headers=auth_headers())
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["total"] >= 1

    user = next(item for item in body["users"] if item["username"] == "alice")
    assert user["chat_id"] == 123
    assert user["referrer"] == "ref"
    assert user["total_keys"] == 1
    assert user["active_keys"] == 1
    assert user["has_trial_key"] is True
    assert user["total_payments"] == 1
    assert user["paid_payments"] == 1
    assert user["paid_amount"] == 250
    assert user["last_payment_at"] is not None
    assert user["last_key_expires_at"] is not None


@pytest.mark.usefixtures("configured_env")
def test_users_summary_requires_token(api_app):
    response = api_app.get("/users/summary")
    assert response.status_code == 401
