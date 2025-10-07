"""Helpers for working with the local Xray configuration."""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

XRAY_CONFIG = Path(os.getenv("XRAY_CONFIG", "/usr/local/etc/xray/config.json"))
XRAY_SERVICE = os.getenv("XRAY_SERVICE", "xray")


def _load() -> dict[str, Any]:
    if not XRAY_CONFIG.exists():
        raise FileNotFoundError(str(XRAY_CONFIG))
    with XRAY_CONFIG.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def _save(cfg: dict[str, Any]) -> None:
    tmp = XRAY_CONFIG.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(cfg, fh, ensure_ascii=False, indent=2)
    tmp.replace(XRAY_CONFIG)


def _restart() -> None:
    subprocess.run(["systemctl", "restart", XRAY_SERVICE], check=False)


def add_client(email: str) -> dict[str, str]:
    cfg = _load()
    inbounds = cfg.get("inbounds") or []
    if not inbounds:
        raise RuntimeError("XRAY config has no inbounds section")
    inbound = inbounds[0]
    settings = inbound.setdefault("settings", {})
    clients = settings.setdefault("clients", [])
    from uuid import uuid4

    new_uuid = str(uuid4())
    clients.append({"id": new_uuid, "level": 0, "email": email})
    _save(cfg)
    _restart()
    return {"uuid": new_uuid}


def remove_client(client_uuid: str) -> bool:
    cfg = _load()
    inbounds = cfg.get("inbounds") or []
    if not inbounds:
        return False
    inbound = inbounds[0]
    settings = inbound.setdefault("settings", {})
    clients = settings.setdefault("clients", [])
    before = len(clients)
    clients[:] = [c for c in clients if c.get("id") != client_uuid]
    if len(clients) == before:
        return False
    _save(cfg)
    _restart()
    return True
