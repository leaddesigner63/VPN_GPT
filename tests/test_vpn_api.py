from __future__ import annotations

import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import sys
import types


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pytest
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


def test_issue_key_updates_existing_user(test_app):
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
    assert second.status_code == 200

    first_uuid = first.json()["uuid"]
    second_uuid = second.json()["uuid"]

    assert first_uuid != second_uuid
    assert len(add_recorder.calls) == 2
    record = _fetch_record(db_module, "charlie")
    assert record["uuid"] == second_uuid


def test_issue_key_activation_happens_after_xray(test_app, monkeypatch):
    client, _, add_recorder, _ = test_app

    import api.endpoints.vpn as vpn_module

    real_connect = vpn_module.connect
    operations: list[tuple] = []

    @contextmanager
    def tracking_connect(*, autocommit=True):
        with real_connect(autocommit=autocommit) as real_conn:
            class TrackingConnection:
                def __init__(self, inner):
                    self._inner = inner

                def execute(self, sql, params=()):
                    operations.append(("sql", " ".join(sql.split()), params))
                    return self._inner.execute(sql, params)

                def commit(self):
                    operations.append(("commit",))
                    return self._inner.commit()

                def rollback(self):
                    operations.append(("rollback",))
                    return self._inner.rollback()

                def __getattr__(self, item):
                    return getattr(self._inner, item)

            yield TrackingConnection(real_conn)

    monkeypatch.setattr(vpn_module, "connect", tracking_connect)

    def side_effect(uuid_value, username):
        operations.append(("xray", uuid_value, username))

    add_recorder.side_effect = side_effect

    response = client.post(
        "/vpn/issue_key",
        json={"username": "flow"},
        headers=_auth_headers(),
    )

    assert response.status_code == 200

    insert_idx = next(
        idx
        for idx, op in enumerate(operations)
        if op[0] == "sql" and "INSERT INTO vpn_keys" in op[1] and "VALUES" in op[1] and " 0" in op[1]
    )
    xray_idx = next(idx for idx, op in enumerate(operations) if op[0] == "xray")
    activate_idx = next(
        idx for idx, op in enumerate(operations) if op[0] == "sql" and "UPDATE vpn_keys SET active=1" in op[1]
    )

    assert insert_idx < xray_idx < activate_idx


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


def test_health_endpoint_available(monkeypatch):
    _write_env(Path("."))

    fake_response = types.SimpleNamespace(status_code=200, text="")

    def _fake_post(*args, **kwargs):
        return fake_response

    monkeypatch.setitem(sys.modules, "requests", types.SimpleNamespace(post=_fake_post))

    from api.main import app

    client = TestClient(app)
    try:
        response = client.get("/healthz")
        assert response.status_code == 200
        assert response.json() == {"ok": True}
    finally:
        client.close()
