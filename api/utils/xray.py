from __future__ import annotations




import json
import os
import subprocess
from pathlib import Path
from uuid import uuid4

from api.utils.logging import get_logger

XRAY_CONFIG = Path(os.getenv("XRAY_CONFIG", "/usr/local/etc/xray/config.json"))
XRAY_SERVICE = os.getenv("XRAY_SERVICE", "xray")

logger = get_logger("xray")


def _load() -> dict:
    logger.debug("Loading Xray configuration from %s", XRAY_CONFIG)
    with open(XRAY_CONFIG, "r", encoding="utf-8") as fh:
        return json.load(fh)


def _save(cfg: dict) -> None:
    tmp = XRAY_CONFIG.with_suffix(XRAY_CONFIG.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, XRAY_CONFIG)
    logger.info("Saved updated Xray configuration to %s", XRAY_CONFIG)


def _restart() -> None:
    logger.info("Restarting Xray service '%s'", XRAY_SERVICE)
    subprocess.run(["systemctl", "restart", XRAY_SERVICE], check=True)


def add_client(email: str, client_id: str | None = None) -> dict:
    cfg = _load()
    inb = cfg.get("inbounds", [])[0]
    clients = inb.setdefault("settings", {}).setdefault("clients", [])

    # Deduplicate any existing entries that accidentally share the same client id.
    deduped: list[dict] = []
    seen_ids: set[str] = set()
    for client in clients:
        cid = client.get("id")
        if cid and cid in seen_ids:
            logger.warning("Removed duplicate client id from Xray config", extra={"client_id": cid})
            continue
        if cid:
            seen_ids.add(cid)
        deduped.append(client)

    if len(deduped) != len(clients):
        inb["settings"]["clients"] = clients = deduped

    new_uuid = client_id or str(uuid4())
    if new_uuid in seen_ids:
        logger.error("Attempted to add duplicate client id", extra={"client_id": new_uuid, "email": email})
        raise ValueError(f"Client id {new_uuid} already exists")

    clients.append({"id": new_uuid, "level": 0, "email": email})
    _save(cfg)
    _restart()
    logger.info("Added new Xray client", extra={"email": email, "uuid": new_uuid})
    return {"uuid": new_uuid}


def remove_client(uuid: str) -> bool:
    cfg = _load()
    inb = cfg.get("inbounds", [])[0]
    clients = inb.setdefault("settings", {}).setdefault("clients", [])
    before = len(clients)
    inb["settings"]["clients"] = [c for c in clients if c.get("id") != uuid]
    _save(cfg)
    _restart()
    removed = len(inb["settings"]["clients"]) < before
    if removed:
        logger.info("Removed Xray client %s", uuid)
    else:
        logger.warning("Attempted to remove unknown Xray client %s", uuid)
    return removed
