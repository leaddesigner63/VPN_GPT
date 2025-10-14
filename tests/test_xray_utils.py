from __future__ import annotations

import json
from pathlib import Path

import importlib
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _write_config(path: Path, clients: list[dict]) -> None:
    config = {
        "inbounds": [
            {
                "protocol": "vless",
                "settings": {
                    "clients": clients,
                },
            }
        ]
    }
    path.write_text(json.dumps(config), encoding="utf-8")


def _load_config(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _reload_xray(monkeypatch, config_path: Path):
    monkeypatch.setenv("XRAY_CONFIG", str(config_path))
    monkeypatch.setenv("XRAY_SERVICE", "xray-test")

    import api.utils.xray as xray_module

    module = importlib.reload(xray_module)
    restarts: list[None] = []

    def fake_restart() -> None:
        restarts.append(None)

    monkeypatch.setattr(module, "_restart", fake_restart)
    return module, restarts


def test_add_client_replaces_existing_email(tmp_path, monkeypatch):
    config_path = tmp_path / "config.json"
    _write_config(
        config_path,
        [
            {"id": "old-alice", "level": 0, "email": "alice"},
            {"id": "bob-uuid", "level": 0, "email": "bob"},
        ],
    )

    xray, restarts = _reload_xray(monkeypatch, config_path)

    changed = xray.add_client_no_duplicates("new-alice", "alice")

    assert changed is True
    assert len(restarts) == 1

    clients = _load_config(config_path)["inbounds"][0]["settings"]["clients"]
    alice_clients = [client for client in clients if client.get("email") == "alice"]

    assert alice_clients == [{"id": "new-alice", "level": 0, "email": "alice"}]
    assert any(client.get("email") == "bob" for client in clients)


def test_add_client_appends_new_email(tmp_path, monkeypatch):
    config_path = tmp_path / "config.json"
    _write_config(config_path, [{"id": "bob-uuid", "level": 0, "email": "bob"}])

    xray, restarts = _reload_xray(monkeypatch, config_path)

    changed = xray.add_client_no_duplicates("alice-uuid", "alice")

    assert changed is True
    assert len(restarts) == 1

    clients = _load_config(config_path)["inbounds"][0]["settings"]["clients"]
    emails = [client.get("email") for client in clients]
    assert emails.count("alice") == 1
    assert emails.count("bob") == 1


def test_add_client_deduplicates_existing_entries(tmp_path, monkeypatch):
    config_path = tmp_path / "config.json"
    _write_config(
        config_path,
        [
            {"id": "old-alice", "level": 0, "email": "alice"},
            {"id": "current-alice", "level": 0, "email": "alice"},
        ],
    )

    xray, restarts = _reload_xray(monkeypatch, config_path)

    changed = xray.add_client_no_duplicates("current-alice", "alice")

    assert changed is True
    assert len(restarts) == 1

    clients = _load_config(config_path)["inbounds"][0]["settings"]["clients"]
    assert clients == [{"id": "current-alice", "level": 0, "email": "alice"}]


def test_add_client_normalises_email_and_case(tmp_path, monkeypatch):
    config_path = tmp_path / "config.json"
    _write_config(
        config_path,
        [
            {"id": "old-alice", "level": 0, "email": "  Alice  "},
            {"id": "bob-uuid", "level": 0, "email": "bob"},
        ],
    )

    xray, restarts = _reload_xray(monkeypatch, config_path)

    changed = xray.add_client_no_duplicates("new-alice", "alice")

    assert changed is True
    assert len(restarts) == 1

    clients = _load_config(config_path)["inbounds"][0]["settings"]["clients"]
    assert {client["email"] for client in clients} == {"alice", "bob"}
    assert any(client["id"] == "new-alice" for client in clients if client["email"] == "alice")


def test_restart_uses_normalised_service_name(monkeypatch):
    monkeypatch.setenv("XRAY_SERVICE", "xray.service   # comment to ignore")

    import api.utils.xray as xray_module

    module = importlib.reload(xray_module)

    calls: list[list[str]] = []

    def fake_run(cmd, check):  # pragma: no cover - signature compatibility
        calls.append(cmd)

    monkeypatch.setattr(module.subprocess, "run", fake_run)

    module._restart()

    assert calls == [["systemctl", "restart", "xray.service"]]
    assert module.XRAY_SERVICE == "xray.service"


def test_restart_falls_back_to_alternative_names(monkeypatch):
    monkeypatch.setenv("XRAY_SERVICE", "xray.service   # comment to ignore")

    import api.utils.xray as xray_module

    module = importlib.reload(xray_module)

    calls: list[list[str]] = []

    def fake_run(cmd, check):  # pragma: no cover - signature compatibility
        calls.append(cmd)
        if cmd[-1] == "xray.service":
            raise subprocess.CalledProcessError(1, cmd)

    monkeypatch.setattr(module.subprocess, "run", fake_run)

    module._restart()

    assert calls == [
        ["systemctl", "restart", "xray.service"],
        ["systemctl", "restart", "xray"],
    ]
