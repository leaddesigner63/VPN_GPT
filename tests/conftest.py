from __future__ import annotations

import json
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def configure_test_xray(tmp_path, monkeypatch):
    config_path = tmp_path / "xray-config.json"
    config_payload = {
        "inbounds": [
            {
                "protocol": "vless",
                "settings": {"clients": []},
            }
        ]
    }
    config_path.write_text(json.dumps(config_payload), encoding="utf-8")

    monkeypatch.setenv("XRAY_CONFIG", str(config_path))
    monkeypatch.setenv("XRAY_SERVICE", "xray-test")

    from api.utils import xray as xray_module

    monkeypatch.setattr(xray_module, "XRAY_CONFIG", Path(config_path))
    monkeypatch.setattr(xray_module, "XRAY_SERVICE", "xray-test")

    restarts: list[None] = []

    def fake_restart() -> None:
        restarts.append(None)

    monkeypatch.setattr(xray_module, "_restart", fake_restart)

    yield
