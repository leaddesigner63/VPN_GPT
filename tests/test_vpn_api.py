from __future__ import annotations

import sys
import uuid
from importlib import reload
from pathlib import Path
from typing import Callable

import pytest
from fastapi.testclient import TestClient


ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))


class _CallRecorder:
    def __init__(self, side_effect: Callable | None = None):
        self.calls: list[tuple[tuple, dict]] = []
        self.side_effect = side_effect

    def __call__(self, *args, **kwargs):  # pragma: no cover - passthrough
        self.calls.append((args, kwargs))
        if self.side_effect:
            return self.side_effect(*args, **kwargs)


@pytest.fixture
def test_app(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("DATABASE", str(db_path))
    monkeypatch.setenv("ADMIN_TOKEN", "secret")

    import api.utils.db as db_module

    reload(db_module)
    db_module.init_db()

    import api.endpoints.vpn as vpn_module

    reload(vpn_module)

    add_client_recorder = _CallRecorder()
    remove_client_recorder = _CallRecorder()

    monkeypatch.setattr(vpn_module.xray, "add_client", add_client_recorder)
    monkeypatch.setattr(vpn_module.xray, "remove_client", remove_client_recorder)
    monkeypatch.setattr(
        vpn_module,
        "_compose_link_safe",
        lambda uuid, email: f"vless://{uuid}@{email}",
    )

    from fastapi import FastAPI

    app = FastAPI()
    app.include_router(vpn_module.router, prefix="/vpn")

    client = TestClient(app)
    try:
        yield client, add_client_recorder, remove_client_recorder, db_module
    finally:
        client.close()


def _fetch_uuid(db_module, email: str):
    with db_module.connect() as conn:
        row = conn.execute(
            "SELECT uuid FROM vpn_keys WHERE LOWER(username)=?", (email.lower(),)
        ).fetchone()
    return None if row is None else row["uuid"]


def test_issue_key_creates_new_entry(test_app):
    client, add_client, _, db_module = test_app

    response = client.post("/vpn/issue_key", json={"email": "alice@example.com"})

    assert response.status_code == 200
    body = response.json()
    assert body == {
        "email": "alice@example.com",
        "key": body["key"],
        "created": True,
    }
    uuid.UUID(body["key"])

    assert len(add_client.calls) == 1
    assert add_client.calls[0][1] == {"email": "alice@example.com", "client_id": body["key"]}

    assert _fetch_uuid(db_module, "alice@example.com") == body["key"]


def test_issue_key_is_idempotent(test_app):
    client, add_client, _, _ = test_app

    first = client.post("/vpn/issue_key", json={"email": "bob@example.com"})
    second = client.post("/vpn/issue_key", json={"email": "bob@example.com"})

    assert first.status_code == 200
    assert second.status_code == 200

    first_body = first.json()
    second_body = second.json()

    assert first_body["created"] is True
    assert second_body["created"] is False
    assert first_body["key"] == second_body["key"]
    assert len(add_client.calls) == 1


def test_issue_key_invalid_email_returns_400(test_app):
    client, _, _, _ = test_app

    response = client.post("/vpn/issue_key", json={"email": "not-an-email"})

    assert response.status_code == 400
    assert response.json()["detail"] == "invalid email format"


def test_get_key_success(test_app):
    client, _, _, _ = test_app

    created = client.post("/vpn/issue_key", json={"email": "carol@example.com"})
    key = created.json()["key"]

    lookup = client.get("/vpn/key", params={"email": "carol@example.com"})

    assert lookup.status_code == 200
    assert lookup.json() == {"email": "carol@example.com", "key": key}


def test_get_key_not_found_returns_404(test_app):
    client, _, _, _ = test_app

    response = client.get("/vpn/key", params={"email": "unknown@example.com"})

    assert response.status_code == 404
    assert response.json()["detail"] == "key not found"


def test_issue_key_rolls_back_on_xray_failure(test_app, monkeypatch):
    client, add_client, _, db_module = test_app

    def fail(**_: str):  # pragma: no cover - triggered deliberately
        raise RuntimeError("boom")

    add_client.side_effect = fail

    response = client.post("/vpn/issue_key", json={"email": "dave@example.com"})

    assert response.status_code == 502
    assert response.json()["detail"] == "failed to sync key with xray"
    assert _fetch_uuid(db_module, "dave@example.com") is None


def test_issue_key_updates_existing_blank_uuid(test_app):
    client, add_client, _, db_module = test_app

    with db_module.connect() as conn:
        conn.execute(
            "INSERT INTO vpn_keys (username, uuid, active) VALUES (?, '', 1)",
            ("eve@example.com",),
        )

    response = client.post("/vpn/issue_key", json={"email": "EVE@example.com"})

    assert response.status_code == 200
    body = response.json()
    assert body["created"] is True
    assert _fetch_uuid(db_module, "eve@example.com") == body["key"]
    assert len(add_client.calls) == 1


def test_issue_missing_keys_requires_admin_token(test_app):
    client, _, _, _ = test_app

    response = client.post("/vpn/issue_missing_keys")

    assert response.status_code == 401
    assert response.json()["detail"] == "unauthorized"


def test_issue_missing_keys_issues_all(test_app):
    client, add_client, _, db_module = test_app

    with db_module.connect() as conn:
        conn.execute(
            "INSERT INTO vpn_keys (username, uuid, active) VALUES (?, NULL, 1)",
            ("foo@example.com",),
        )
        conn.execute(
            "INSERT INTO vpn_keys (username, uuid, active) VALUES (?, '', 1)",
            ("bar@example.com",),
        )

    response = client.post(
        "/vpn/issue_missing_keys", headers={"X-Admin-Token": "secret"}
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload == {"issued": 2, "skipped": 0}
    assert len(add_client.calls) == 2

    assert _fetch_uuid(db_module, "foo@example.com")
    assert _fetch_uuid(db_module, "bar@example.com")

