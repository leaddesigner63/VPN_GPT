from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in os.sys.path:
    os.sys.path.insert(0, str(ROOT))


@dataclass
class EnvConfig:
    database: Path
    env_file: Path


@pytest.fixture()
def configured_env(tmp_path, monkeypatch) -> EnvConfig:
    db_path = tmp_path / "test.db"
    env_path = tmp_path / ".env"
    env_path.write_text(
        "\n".join(
            [
                "VLESS_HOST=test.example",
                "VLESS_PORT=2053",
                "BOT_PAYMENT_URL=https://vpn-gpt.store/pay",
                "TRIAL_DAYS=3",
                "PLANS=1m:180,3m:450",
                "ADMIN_TOKEN=secret",
                "INTERNAL_TOKEN=service",
                "ADMIN_PANEL_PASSWORD=panelpass",
                "REFERRAL_BONUS_DAYS=30",
            ]
        ),
        encoding="utf-8",
    )

    monkeypatch.setenv("ENV_PATH", str(env_path))
    monkeypatch.setenv("DATABASE", str(db_path))
    monkeypatch.setenv("BOT_TOKEN", "test-token")
    monkeypatch.setenv("GPT_API_KEY", "test-key")

    import api.config as config_module
    import importlib

    importlib.reload(config_module)
    import api.utils.db as db_module

    importlib.reload(db_module)
    db_module.init_db()

    yield EnvConfig(database=db_path, env_file=env_path)


@pytest.fixture()
def api_app(configured_env, monkeypatch) -> TestClient:
    import api.main as api_main
    import importlib

    importlib.reload(api_main)

    app = FastAPI()
    app.include_router(api_main.vpn.router)
    app.include_router(api_main.payments.router)
    app.include_router(api_main.users.router)
    app.include_router(api_main.referrals.router)

    client = TestClient(app)
    try:
        yield client
    finally:
        client.close()


def auth_headers() -> dict[str, str]:
    return {"Authorization": "Bearer service"}


def _fetch_keys(database: Path, username: str) -> list[sqlite3.Row]:
    con = sqlite3.connect(database)
    try:
        con.row_factory = sqlite3.Row
        cur = con.execute("SELECT * FROM vpn_keys WHERE username=?", (username,))
        return cur.fetchall()
    finally:
        con.close()


def _fetch_user(database: Path, username: str) -> sqlite3.Row | None:
    con = sqlite3.connect(database)
    try:
        con.row_factory = sqlite3.Row
        cur = con.execute("SELECT * FROM tg_users WHERE username=?", (username,))
        return cur.fetchone()
    finally:
        con.close()


def _fetch_referral(database: Path, referrer: str, referee: str) -> sqlite3.Row | None:
    con = sqlite3.connect(database)
    try:
        con.row_factory = sqlite3.Row
        cur = con.execute(
            "SELECT * FROM referrals WHERE referrer=? AND referee=?",
            (referrer, referee),
        )
        return cur.fetchone()
    finally:
        con.close()


def test_issue_trial_key(api_app, configured_env):
    response = api_app.post(
        "/vpn/issue_key",
        json={"username": "alice", "chat_id": 12345},
        headers=auth_headers(),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["trial"] is True
    assert body["reused"] is False

    keys = _fetch_keys(configured_env.database, "alice")
    assert len(keys) == 1
    assert keys[0]["trial"] == 1

    config_path = Path(os.environ["XRAY_CONFIG"])
    config = json.loads(config_path.read_text(encoding="utf-8"))
    clients = config["inbounds"][0]["settings"]["clients"]
    assert any(
        client.get("id") == body["uuid"] and client.get("email") == "VPN_GPT_alice"
        for client in clients
    )


def test_issue_trial_second_time_returns_existing(api_app):
    first = api_app.post(
        "/vpn/issue_key",
        json={"username": "bob", "chat_id": 1},
        headers=auth_headers(),
    )
    second = api_app.post(
        "/vpn/issue_key",
        json={"username": "bob", "chat_id": 1},
        headers=auth_headers(),
    )

    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json()["reused"] is True


def test_trial_unavailable_after_consumption(api_app):
    api_app.post(
        "/vpn/issue_key",
        json={"username": "carol"},
        headers=auth_headers(),
    )
    response = api_app.post(
        "/vpn/issue_key",
        json={"username": "carol", "trial": True},
        headers=auth_headers(),
    )
    assert response.status_code == 200
    body = response.json()
    assert body["reused"] is True


def test_renew_creates_new_key(api_app, configured_env):
    response = api_app.post(
        "/vpn/renew_key",
        json={"username": "dave", "plan": "1m"},
        headers=auth_headers(),
    )

    assert response.status_code == 200
    keys = _fetch_keys(configured_env.database, "dave")
    assert len(keys) == 1
    assert keys[0]["trial"] == 0


def test_admin_auth_endpoint(configured_env):
    import api.main as api_main
    import importlib

    importlib.reload(api_main)

    with TestClient(api_main.app) as client:
        ok = client.post("/admin/auth", json={"password": "panelpass"})
        assert ok.status_code == 200
        body = ok.json()
        assert body["ok"] is True
        assert body["admin_token"] == "secret"

        bad = client.post("/admin/auth", json={"password": "wrong"})
        assert bad.status_code == 401
        assert bad.json()["detail"] == "Неверный пароль"


def test_payment_confirmation_extends_subscription(api_app, configured_env):
    # Issue initial key via renewal to avoid trial flag
    api_app.post(
        "/vpn/renew_key",
        json={"username": "eve", "plan": "1m", "chat_id": 42},
        headers=auth_headers(),
    )

    create = api_app.post(
        "/payments/create",
        json={"username": "eve", "plan": "1m", "chat_id": 42},
        headers=auth_headers(),
    )
    payment_id = create.json()["payment_id"]

    confirm = api_app.post(
        "/payments/confirm",
        json={"payment_id": payment_id, "username": "eve", "plan": "1m", "chat_id": 42},
        headers=auth_headers(),
    )

    assert confirm.status_code == 200
    body = confirm.json()
    assert body["status"] == "paid"
    assert body["key_uuid"]

    keys = _fetch_keys(configured_env.database, "eve")
    assert len(keys) == 1
    assert keys[0]["trial"] == 0


def test_create_payment_rejects_invalid_referrer(api_app, configured_env):
    response = api_app.post(
        "/payments/create",
        json={
            "username": "mallory",
            "plan": "1m",
            "chat_id": 99,
            "referrer": "   ",
        },
        headers=auth_headers(),
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "invalid_referrer"


def test_use_referral_records_referrer(api_app, configured_env):
    response = api_app.post(
        "/referral/use",
        json={"referrer": "Alice", "referee": "Bob", "chat_id": 555},
        headers=auth_headers(),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["referrer"] == "Alice"
    assert body["referee"] == "Bob"
    assert body["already_exists"] is False

    user = _fetch_user(configured_env.database, body["referee"])
    assert user is not None
    assert user["referrer"] == body["referrer"]
    assert user["chat_id"] == 555

    repeat = api_app.post(
        "/referral/use",
        json={"referrer": "Alice", "referee": "Bob"},
        headers=auth_headers(),
    )
    assert repeat.status_code == 200
    assert repeat.json()["already_exists"] is True


def test_use_referral_rejects_conflicting_referrer(api_app, configured_env):
    api_app.post(
        "/referral/use",
        json={"referrer": "alice", "referee": "bob"},
        headers=auth_headers(),
    )

    conflict = api_app.post(
        "/referral/use",
        json={"referrer": "carol", "referee": "bob"},
        headers=auth_headers(),
    )

    assert conflict.status_code == 409
    assert conflict.json()["detail"] == "referrer_already_set"


def test_referral_bonus_awarded_on_payment(api_app, configured_env):
    api_app.post(
        "/referral/use",
        json={"referrer": "alice", "referee": "bob", "chat_id": 321},
        headers=auth_headers(),
    )

    create = api_app.post(
        "/payments/create",
        json={"username": "bob", "plan": "1m", "chat_id": 123},
        headers=auth_headers(),
    )
    assert create.status_code == 200
    payment_id = create.json()["payment_id"]

    confirm = api_app.post(
        "/payments/confirm",
        json={"payment_id": payment_id, "username": "bob", "plan": "1m"},
        headers=auth_headers(),
    )

    assert confirm.status_code == 200
    referral_record = _fetch_referral(configured_env.database, "alice", "bob")
    assert referral_record is not None
    assert referral_record["bonus_days"] == 30

    referrer_keys = _fetch_keys(configured_env.database, "alice")
    assert len(referrer_keys) == 1
    assert referrer_keys[0]["trial"] == 0


def test_issue_key_returns_503_when_service_token_missing(tmp_path, monkeypatch):
    db_path = tmp_path / "token-missing.db"
    env_path = tmp_path / ".env"
    env_path.write_text(
        "\n".join(
            [
                "VLESS_HOST=test.example",
                "VLESS_PORT=2053",
                "BOT_PAYMENT_URL=https://vpn-gpt.store/pay",
                "TRIAL_DAYS=3",
                "PLANS=1m:180",
            ]
        ),
        encoding="utf-8",
    )

    monkeypatch.setenv("ENV_PATH", str(env_path))
    monkeypatch.setenv("DATABASE", str(db_path))
    monkeypatch.delenv("ADMIN_TOKEN", raising=False)
    monkeypatch.delenv("INTERNAL_TOKEN", raising=False)

    import importlib

    import api.config as config_module
    importlib.reload(config_module)

    import api.endpoints.security as security_module
    importlib.reload(security_module)

    import api.utils.db as db_module
    importlib.reload(db_module)
    db_module.init_db()

    import api.main as api_main

    importlib.reload(api_main)

    app = FastAPI()
    app.include_router(api_main.vpn.router)

    client = TestClient(app)
    try:
        response = client.post(
            "/vpn/issue_key",
            json={"username": "zoe"},
        )
    finally:
        client.close()

    assert response.status_code == 503
    assert response.json() == {"detail": "service_token_not_configured"}


def test_auto_update_adds_trial_column(tmp_path):
    legacy_db = tmp_path / "legacy.db"
    con = sqlite3.connect(legacy_db)
    try:
        con.execute(
            """
            CREATE TABLE vpn_keys (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL,
                chat_id INTEGER,
                uuid TEXT NOT NULL UNIQUE,
                link TEXT NOT NULL,
                label TEXT,
                country TEXT,
                active INTEGER NOT NULL DEFAULT 1,
                issued_at TEXT NOT NULL,
                expires_at TEXT NOT NULL
            )
            """
        )
        con.execute(
            """
            INSERT INTO vpn_keys (username, chat_id, uuid, link, label, country, active, issued_at, expires_at)
            VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)
            """,
            (
                "legacy",
                123,
                "uuid-legacy",
                "https://example.com",
                "Legacy",
                "US",
                "2024-01-01T00:00:00",
                "2024-02-01T00:00:00",
            ),
        )
        con.commit()
    finally:
        con.close()

    import api.utils.db as db_module

    db_module.auto_update_missing_fields(db_path=legacy_db)

    con = sqlite3.connect(legacy_db)
    try:
        con.row_factory = sqlite3.Row
        columns = {
            row["name"]
            for row in con.execute("PRAGMA table_info(vpn_keys)").fetchall()
        }
        assert "trial" in columns

        cur = con.execute("SELECT trial FROM vpn_keys WHERE username=?", ("legacy",))
        row = cur.fetchone()
        assert row is not None
        assert row["trial"] == 0
    finally:
        con.close()


def test_issue_key_auto_migrates_missing_trial_column(tmp_path, monkeypatch):
    db_path = tmp_path / "legacy.db"
    env_path = tmp_path / ".env"
    env_path.write_text(
        "\n".join(
            [
                "VLESS_HOST=test.example",
                "VLESS_PORT=2053",
                "BOT_PAYMENT_URL=https://vpn-gpt.store/pay",
                "TRIAL_DAYS=3",
                "PLANS=1m:180,3m:450",
                "ADMIN_TOKEN=secret",
                "INTERNAL_TOKEN=service",
                "REFERRAL_BONUS_DAYS=30",
            ]
        ),
        encoding="utf-8",
    )

    monkeypatch.setenv("ENV_PATH", str(env_path))
    monkeypatch.setenv("DATABASE", str(db_path))
    monkeypatch.setenv("BOT_TOKEN", "test-token")

    with sqlite3.connect(db_path) as con:
        con.executescript(
            """
            CREATE TABLE tg_users (
              username  TEXT PRIMARY KEY,
              chat_id   INTEGER NOT NULL,
              referrer  TEXT,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );

            CREATE TABLE vpn_keys (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              username TEXT NOT NULL,
              chat_id INTEGER,
              uuid TEXT NOT NULL UNIQUE,
              link TEXT NOT NULL,
              label TEXT,
              country TEXT,
              active INTEGER NOT NULL DEFAULT 1,
              issued_at TEXT NOT NULL,
              expires_at TEXT NOT NULL
            );
            """
        )

    import api.config as config_module
    import importlib

    importlib.reload(config_module)
    import api.utils.db as db_module

    importlib.reload(db_module)

    import api.endpoints.security as security_module

    importlib.reload(security_module)

    import api.main as api_main

    importlib.reload(api_main)

    app = FastAPI()
    app.include_router(api_main.vpn.router)
    app.include_router(api_main.users.router)

    client = TestClient(app)
    try:
        response = client.post(
            "/vpn/issue_key",
            json={"username": "legacy_user", "chat_id": 99},
            headers=auth_headers(),
        )
    finally:
        client.close()

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["trial"] is True

    with sqlite3.connect(db_path) as con:
        cur = con.execute("PRAGMA table_info(vpn_keys)")
        columns = {row[1] for row in cur.fetchall()}

    assert "trial" in columns
