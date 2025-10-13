from __future__ import annotations

import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

import sys
import types


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pytest
import sqlite3
from fastapi import FastAPI
from fastapi.testclient import TestClient


@dataclass
class CallRecord:
    args: tuple
    kwargs: dict


class Recorder:
    def __init__(self, side_effect: Callable | None = None) -> None:
        self.calls: list[CallRecord] = []
        self.side_effect = side_effect

    def __call__(self, *args, **kwargs):
        self.calls.append(CallRecord(args=args, kwargs=kwargs))
        if self.side_effect:
            return self.side_effect(*args, **kwargs)


def _write_env(tmp_path: Path) -> None:
    env_path = Path("/root/VPN_GPT/.env")
    env_path.parent.mkdir(parents=True, exist_ok=True)
    env_path.write_text("VLESS_HOST=example.com\nVLESS_PORT=443\n", encoding="utf-8")


@pytest.fixture()
def test_app(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("DATABASE", str(db_path))
    monkeypatch.setenv("ADMIN_TOKEN", "secret")
    monkeypatch.setenv("XRAY_CONFIG", str(tmp_path / "config.json"))
    monkeypatch.setenv("XRAY_SERVICE", "xray-test")

    _write_env(tmp_path)

    import api.utils.db as db_module

    from importlib import reload

    reload(db_module)
    db_module.init_db()

    import api.endpoints.vpn as vpn_module

    reload(vpn_module)

    add_recorder = Recorder()
    remove_recorder = Recorder()

    monkeypatch.setattr(vpn_module.xray, "add_client_no_duplicates", add_recorder)
    monkeypatch.setattr(vpn_module.xray, "remove_client", remove_recorder)
    monkeypatch.setattr(vpn_module, "build_vless_link", lambda uuid_value, username: f"vless://{uuid_value}@{username}")

    app = FastAPI()
    app.include_router(vpn_module.router, prefix="/vpn")

    client = TestClient(app)
    try:
        yield client, db_module, add_recorder, remove_recorder
    finally:
        client.close()


def _auth_headers() -> dict[str, str]:
    return {"Authorization": "Bearer secret"}


def _fetch_record(db_module, username: str) -> dict | None:
    from api.utils.db import connect

    with connect() as conn:
        cur = conn.execute(
            "SELECT username, uuid, link, active FROM vpn_keys WHERE username=?", (username,),
        )
        row = cur.fetchone()
    return None if row is None else dict(row)


def test_issue_key_creates_record(test_app):
    client, db_module, add_recorder, _ = test_app

    response = client.post(
        "/vpn/issue_key",
        json={"username": "alice"},
        headers=_auth_headers(),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["username"] == "alice"
    uuid.UUID(body["uuid"])
    assert body["link"].startswith("vless://")

    assert len(add_recorder.calls) == 1
    assert add_recorder.calls[0].args == (body["uuid"], "alice")

    record = _fetch_record(db_module, "alice")
    assert record is not None
    assert record["uuid"] == body["uuid"]
    assert record["active"] == 1


def test_issue_key_requires_authorization(test_app):
    client, *_ = test_app

    response = client.post("/vpn/issue_key", json={"username": "bob"})

    assert response.status_code == 401
    assert response.json()["detail"] == "unauthorized"


def test_issue_key_accepts_x_admin_header(test_app):
    client, *_ = test_app

    response = client.post(
        "/vpn/issue_key",
        json={"username": "bianca"},
        headers={"X-Admin-Token": "secret"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["username"] == "bianca"


def test_issue_key_rejects_user_with_active_key(test_app):
    client, db_module, add_recorder, _ = test_app

    first = client.post(
        "/vpn/issue_key",
        json={"username": "charlie", "days": 5},
        headers=_auth_headers(),
    )
    second = client.post(
        "/vpn/issue_key",
        json={"username": "@charlie"},
        headers=_auth_headers(),
    )

    assert first.status_code == 200
    assert second.status_code == 409
    assert second.json() == {"ok": False, "error": "user_has_active_key"}

    assert len(add_recorder.calls) == 1

    record = _fetch_record(db_module, "charlie")
    assert record["uuid"] == first.json()["uuid"]


def test_issue_key_activation_sequence(test_app, monkeypatch):
    client, db_module, add_recorder, _ = test_app

    import api.endpoints.vpn as vpn_module

    recorded_sql: list[tuple[str, tuple]] = []

    class RecordingConnection:
        def __init__(self, wrapped):
            self._wrapped = wrapped

        def execute(self, sql, params=()):
            normalized = " ".join(sql.split())
            recorded_sql.append((normalized, params))
            return self._wrapped.execute(sql, params)

        def __getattr__(self, item):
            return getattr(self._wrapped, item)

    original_connect = vpn_module.connect

    @contextmanager
    def recording_connect(*, autocommit: bool = True):
        with original_connect(autocommit=autocommit) as real_conn:
            yield RecordingConnection(real_conn)

    monkeypatch.setattr(vpn_module, "connect", recording_connect)

    def ensure_inactive_before_activation(uuid_value, username):
        normalized = [sql for sql, _ in recorded_sql]
        inserts = [stmt for stmt in normalized if stmt.startswith("INSERT INTO vpn_keys")]
        assert inserts, "VPN key insert was not recorded"
        assert any(stmt.endswith("active) VALUES (?, ?, ?, ?, ?, 0)") for stmt in inserts)
        assert not any("UPDATE vpn_keys SET active=1" in stmt for stmt in normalized)

    add_recorder.side_effect = ensure_inactive_before_activation

    response = client.post(
        "/vpn/issue_key",
        json={"username": "gloria"},
        headers=_auth_headers(),
    )

    assert response.status_code == 200

    normalized = [sql for sql, _ in recorded_sql]
    insert_index = next(i for i, stmt in enumerate(normalized) if stmt.startswith("INSERT INTO vpn_keys"))
    update_index = next(i for i, stmt in enumerate(normalized) if stmt.startswith("UPDATE vpn_keys SET active=1"))
    assert update_index > insert_index

    record = _fetch_record(db_module, "gloria")
    assert record is not None
    assert record["active"] == 1


def test_issue_key_rolls_back_on_xray_failure(test_app):
    client, db_module, add_recorder, _ = test_app

    import api.endpoints.vpn as vpn_module

    def fail_sync(uuid_value, username):
        raise vpn_module.xray.XrayRestartError("boom")

    add_recorder.side_effect = fail_sync

    response = client.post(
        "/vpn/issue_key",
        json={"username": "henry"},
        headers=_auth_headers(),
    )

    assert response.status_code == 500
    body = response.json()
    assert body["ok"] is False
    assert body["error"] == "xray_restart_failed"

    record = _fetch_record(db_module, "henry")
    assert record is None


def test_renew_key_extends_expiry(test_app):
    client, _, _, _ = test_app

    issued = client.post(
        "/vpn/issue_key",
        json={"username": "dora", "days": 1},
        headers=_auth_headers(),
    ).json()

    renew = client.post(
        "/vpn/renew_key",
        json={"username": "dora", "days": 2},
        headers=_auth_headers(),
    )

    assert renew.status_code == 200
    body = renew.json()
    assert body["ok"] is True
    assert body["username"] == "dora"
    assert body["expires_at"] > issued["expires_at"]


def test_disable_key_deactivates_record(test_app):
    client, db_module, _, remove_recorder = test_app

    issued = client.post(
        "/vpn/issue_key",
        json={"username": "edgar"},
        headers=_auth_headers(),
    ).json()

    disable = client.post(
        "/vpn/disable_key",
        json={"uuid": issued["uuid"]},
        headers=_auth_headers(),
    )

    assert disable.status_code == 200
    assert disable.json() == {"ok": True, "uuid": issued["uuid"]}
    assert len(remove_recorder.calls) == 1
    assert remove_recorder.calls[0].args == (issued["uuid"],)

    record = _fetch_record(db_module, "edgar")
    assert record["active"] == 0


def test_save_vpn_key_starts_inactive(monkeypatch, tmp_path):
    db_path = tmp_path / "legacy.db"

    import utils.db as legacy_db
    from importlib import reload

    legacy_db = reload(legacy_db)
    monkeypatch.setattr(legacy_db, "DB_PATH", str(db_path))

    legacy_db.init_db()

    expires = datetime.utcnow() + timedelta(days=1)
    key_uuid = legacy_db.save_vpn_key(123, "legacy_user", "Legacy User", "vless://example", expires)

    assert key_uuid is not None

    with sqlite3.connect(str(db_path)) as conn:
        cur = conn.execute(
            "SELECT key_uuid, active FROM vpn_keys WHERE username=?", ("legacy_user",)
        )
        row = cur.fetchone()

    assert row is not None
    assert row[0] == key_uuid
    assert row[1] == 0

def test_get_user_returns_keys(test_app):
    client, _, _, _ = test_app

    issued = client.post(
        "/vpn/issue_key",
        json={"username": "frank"},
        headers=_auth_headers(),
    )
    assert issued.status_code == 200

    response = client.get("/vpn/users/frank", headers=_auth_headers())
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["username"] == "frank"
    assert len(body["keys"]) == 1
    assert body["keys"][0]["uuid"] == issued.json()["uuid"]


def test_get_my_key_by_username(test_app):
    client, _, _, _ = test_app

    issued = client.post(
        "/vpn/issue_key",
        json={"username": "irene"},
        headers=_auth_headers(),
    )
    assert issued.status_code == 200

    response = client.get("/vpn/my_key", params={"username": "@irene"})
    assert response.status_code == 200
    body = response.json()
    assert body == {
        "ok": True,
        "username": "irene",
        "uuid": issued.json()["uuid"],
        "link": issued.json()["link"],
        "expires_at": issued.json()["expires_at"].replace("+00:00", "Z"),
        "active": True,
    }


def test_get_my_key_by_chat_id_has_priority(test_app):
    client, db_module, _, _ = test_app

    issued = client.post(
        "/vpn/issue_key",
        json={"username": "julia"},
        headers=_auth_headers(),
    )
    assert issued.status_code == 200

    with db_module.connect() as conn:
        conn.execute(
            "UPDATE vpn_keys SET chat_id=? WHERE username=?", (123456789, "julia")
        )

    response = client.get(
        "/vpn/my_key",
        params={"chat_id": 123456789, "username": "ignored"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["username"] == "julia"
    assert body["uuid"] == issued.json()["uuid"]
    assert body["active"] is True


def test_get_my_key_returns_not_found(test_app):
    client, _, _, _ = test_app

    response = client.get("/vpn/my_key", params={"username": "missing"})
    assert response.status_code == 200
    assert response.json() == {"ok": False, "error": "not_found"}


def test_get_my_key_requires_query_parameter(test_app):
    client, _, _, _ = test_app

    response = client.get("/vpn/my_key")
    assert response.status_code == 422


def test_health_endpoint_available():
    _write_env(Path("."))

    class _DummyResponse:
        def __init__(self) -> None:
            self.status_code = 200
            self.text = ""

    dummy_requests = types.ModuleType("requests")
    dummy_requests.post = lambda *args, **kwargs: _DummyResponse()
    sys.modules["requests"] = dummy_requests

    from api.main import app

    client = TestClient(app)
    try:
        response = client.get("/healthz")
        assert response.status_code == 200
        assert response.json() == {"ok": True}
    finally:
        client.close()
