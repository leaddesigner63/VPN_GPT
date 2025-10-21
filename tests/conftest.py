from __future__ import annotations

import json
import os
import sys
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
    monkeypatch.setenv("GPT_API_KEY", os.getenv("GPT_API_KEY", "test-key"))
    monkeypatch.setenv("RENEWAL_NOTIFICATION_GPT_API_KEY", os.getenv("RENEWAL_NOTIFICATION_GPT_API_KEY", "test-key"))

    project_root = Path(__file__).resolve().parents[1]
    if str(project_root) not in sys.path:
        sys.path.insert(0, str(project_root))

    from api.utils import xray as xray_module

    monkeypatch.setattr(xray_module, "XRAY_CONFIG", Path(config_path))
    monkeypatch.setattr(xray_module, "XRAY_SERVICE", "xray-test")

    restarts: list[None] = []

    def fake_restart() -> None:
        restarts.append(None)

    monkeypatch.setattr(xray_module, "_restart", fake_restart)

    yield
