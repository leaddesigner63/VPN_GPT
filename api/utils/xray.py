from __future__ import annotations




from api.utils.vless import build_vless_link
import json
import os
import subprocess
from pathlib import Path
from uuid import uuid4

XRAY_CONFIG = Path(os.getenv("XRAY_CONFIG", "/usr/local/etc/xray/config.json"))
XRAY_SERVICE = os.getenv("XRAY_SERVICE", "xray")


def _load() -> dict:
    with open(XRAY_CONFIG, "r", encoding="utf-8") as fh:
        return json.load(fh)


def _save(cfg: dict) -> None:
    tmp = XRAY_CONFIG.with_suffix(XRAY_CONFIG.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, XRAY_CONFIG)


def _restart() -> None:
    subprocess.run(["systemctl", "restart", XRAY_SERVICE], check=True)


def add_client(email: str, client_id: str | None = None) -> dict:
    cfg = _load()
    inb = cfg.get("inbounds", [])[0]
    clients = inb.setdefault("settings", {}).setdefault("clients", [])
    new_uuid = client_id or str(uuid4())
    clients.append({"id": new_uuid, "level": 0, "email": email})
    _save(cfg)
    _restart()
    return {"uuid": new_uuid}


def remove_client(uuid: str) -> bool:
    cfg = _load()
    inb = cfg.get("inbounds", [])[0]
    clients = inb.setdefault("settings", {}).setdefault("clients", [])
    before = len(clients)
    inb["settings"]["clients"] = [c for c in clients if c.get("id") != uuid]
    _save(cfg)
    _restart()
    return len(inb["settings"]["clients"]) < before
