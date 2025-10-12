from __future__ import annotations
from importlib import reload
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def main_app_client(tmp_path, monkeypatch):
    db_path = tmp_path / "main_app.db"
    monkeypatch.setenv("DATABASE", str(db_path))
    monkeypatch.setenv("ADMIN_TOKEN", "secret-token")

    import api.utils.db as db_module

    reload(db_module)
    monkeypatch.setattr(db_module, "auto_update_missing_fields", lambda: None)

    import api.endpoints.vpn as vpn_module

    reload(vpn_module)

    mock_safe_add = Mock()
    monkeypatch.setattr(vpn_module, "_safe_add_client", mock_safe_add)
    monkeypatch.setattr(
        vpn_module,
        "compose_vless_link",
        lambda uid, username: f"vless://{username}@{uid}",
    )

    import api.main as main_module

    reload(main_module)

    client_cm = TestClient(main_module.app)
    client = client_cm.__enter__()
    try:
        yield client, mock_safe_add, db_module
    finally:
        client_cm.__exit__(None, None, None)


def test_vpn_routes_available_via_proxy_and_direct_paths(main_app_client):
    client, mock_safe_add, db_module = main_app_client

    proxied_status = client.post(
        "/api/vpn/issue_key",
        json={"username": "proxy-user", "days": 7},
        headers={"x-admin-token": "secret-token"},
    )
    assert proxied_status.status_code == 200

    direct_status = client.post(
        "/vpn/issue_key",
        json={"username": "direct-user", "days": 5},
        headers={"x-admin-token": "secret-token"},
    )
    assert direct_status.status_code == 200

    assert mock_safe_add.call_count == 2

    with db_module.connect() as conn:
        rows = conn.execute(
            "SELECT username FROM vpn_keys WHERE username IN (?, ?)",
            ("proxy-user", "direct-user"),
        ).fetchall()

    stored_usernames = {row["username"] for row in rows}
    assert {"proxy-user", "direct-user"} <= stored_usernames
