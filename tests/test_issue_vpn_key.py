from __future__ import annotations
import sys
from importlib import reload
from pathlib import Path
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))


@pytest.fixture
def test_client(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("DATABASE", str(db_path))
    monkeypatch.setenv("ADMIN_TOKEN", "secret-token")

    import api.utils.db as db_module

    reload(db_module)
    db_module.init_db()

    import api.endpoints.vpn as vpn_module

    reload(vpn_module)

    mock_safe_add = Mock()
    monkeypatch.setattr(vpn_module, "_safe_add_client", mock_safe_add)
    monkeypatch.setattr(
        vpn_module,
        "compose_vless_link",
        lambda uid, username: f"vless://{username}@{uid}",
    )

    warning_spy = Mock()
    monkeypatch.setattr(vpn_module.logger, "warning", warning_spy)

    from fastapi import FastAPI

    app = FastAPI()
    app.include_router(vpn_module.router, prefix="/vpn")

    client = TestClient(app)
    try:
        yield client, mock_safe_add, db_module, warning_spy
    finally:
        client.close()


def test_issue_vpn_key_creates_new(test_client):
    client, mock_safe_add, db_module, warning_spy = test_client

    response = client.post(
        "/vpn/issue_key",
        json={"username": "alice", "days": 7},
        headers={"x-admin-token": "secret-token"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert "uuid" in body
    assert "link" in body
    assert body["message"] == "Ключ создан успешно."
    assert mock_safe_add.call_count == 1
    warning_spy.assert_not_called()

    with db_module.connect() as conn:
        row = conn.execute(
            "SELECT username, active FROM vpn_keys WHERE username=?", ("alice",)
        ).fetchone()

    assert row is not None
    assert row["active"] == 1


def test_issue_vpn_key_conflict_returns_409(test_client):
    client, mock_safe_add, _, warning_spy = test_client

    first_response = client.post(
        "/vpn/issue_key",
        json={"username": "bob", "days": 10},
        headers={"x-admin-token": "secret-token"},
    )
    assert first_response.status_code == 200

    second_response = client.post(
        "/vpn/issue_key",
        json={"username": "bob", "days": 10},
        headers={"x-admin-token": "secret-token"},
    )

    assert second_response.status_code == 409
    assert second_response.json() == {
        "ok": False,
        "error": "active_key_exists",
        "message": "У пользователя уже есть действующий ключ.",
    }
    assert mock_safe_add.call_count == 1
    warning_spy.assert_called_once()
    assert (
        warning_spy.call_args[0][0]
        == "User already has active key — skipping new issue."
    )


def test_issue_vpn_key_updates_inactive_record(test_client):
    client, mock_safe_add, db_module, warning_spy = test_client

    with db_module.connect() as conn:
        conn.execute(
            """
            INSERT INTO vpn_keys (username, uuid, link, issued_at, expires_at, active)
            VALUES (?, ?, ?, ?, ?, 0)
            """,
            ("carol", None, None, None, None),
        )

    response = client.post(
        "/vpn/issue_key", json={"username": "carol", "days": 5}
    )

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["uuid"]
    assert body["link"]

    with db_module.connect() as conn:
        row = conn.execute(
            "SELECT uuid, link, active FROM vpn_keys WHERE username=?",
            ("carol",),
        ).fetchone()

    assert row["active"] == 1
    assert row["uuid"] == body["uuid"]
    assert row["link"] == body["link"]
    assert mock_safe_add.call_count == 1
    warning_spy.assert_not_called()
