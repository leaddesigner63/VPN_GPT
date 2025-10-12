from __future__ import annotations

import sys
from importlib import import_module
from types import ModuleType

import pytest
from fastapi.testclient import TestClient


def reload_main(monkeypatch: pytest.MonkeyPatch, *, root_path: str | None, server_url: str | None) -> ModuleType:
    """Reload ``api.main`` with the provided configuration overrides."""

    module_name = "api.main"
    sys.modules.pop(module_name, None)

    if root_path is None:
        monkeypatch.delenv("API_ROOT_PATH", raising=False)
    else:
        monkeypatch.setenv("API_ROOT_PATH", root_path)

    if server_url is None:
        monkeypatch.delenv("OPENAPI_SERVER_URL", raising=False)
    else:
        monkeypatch.setenv("OPENAPI_SERVER_URL", server_url)

    module = import_module(module_name)
    return module


def test_healthz_available_with_optional_api_prefix(monkeypatch: pytest.MonkeyPatch) -> None:
    module = reload_main(monkeypatch, root_path="", server_url=None)
    client = TestClient(module.app)

    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json()["ok"] is True

    prefixed = client.get("/api/healthz")
    assert prefixed.status_code == 200

    schema = client.get("/openapi.json").json()
    assert schema["servers"] == [{"url": "https://vpn-gpt.store"}]


def test_openapi_server_matches_root_path(monkeypatch: pytest.MonkeyPatch) -> None:
    module = reload_main(monkeypatch, root_path="", server_url=None)
    client = TestClient(module.app)

    response = client.get("/healthz")
    assert response.status_code == 200

    schema = client.get("/openapi.json").json()
    assert schema["servers"] == [{"url": "https://vpn-gpt.store"}]


def test_root_path_configuration_preserves_legacy_routes(monkeypatch: pytest.MonkeyPatch) -> None:
    module = reload_main(monkeypatch, root_path="/api", server_url=None)
    client = TestClient(module.app)

    response = client.get("/api/healthz")
    assert response.status_code == 200

    legacy = client.get("/healthz")
    assert legacy.status_code == 200

    schema = client.get("/openapi.json").json()
    assert schema["servers"] == [{"url": "https://vpn-gpt.store/api"}]

