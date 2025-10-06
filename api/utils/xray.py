import json
import os
import subprocess
from uuid import uuid4

XRAY_CONFIG = os.getenv("XRAY_CONFIG", "/usr/local/etc/xray/config.json")
XRAY_SERVICE = os.getenv("XRAY_SERVICE", "xray")

def _load():
    with open(XRAY_CONFIG, "r", encoding="utf-8") as f:
        return json.load(f)

def _save(cfg: dict):
    tmp = XRAY_CONFIG + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    os.replace(tmp, XRAY_CONFIG)

def _restart():
    # Перезапуск сервиса Xray
    subprocess.run(["systemctl", "restart", XRAY_SERVICE], check=True)

def add_client(email: str) -> dict:
    cfg = _load()
    inb = cfg.get("inbounds", [])[0]
    clients = inb.setdefault("settings", {}).setdefault("clients", [])
    new_uuid = str(uuid4())
    clients.append({"id": new_uuid, "level": 0, "email": email})
    _save(cfg)
    _restart()
    return {"uuid": new_uuid}

def remove_client(uuid: str) -> bool:
    cfg = _load()
    inb = cfg.get("inbounds", [])[0]
    clients = inb.setdefault("settings", {}).setdefault("clients", [])
    before = len(clients)
    clients = [c for c in clients if c.get("id") != uuid]
    inb["settings"]["clients"] = clients
    _save(cfg)
    _restart()
    return len(clients) < before

