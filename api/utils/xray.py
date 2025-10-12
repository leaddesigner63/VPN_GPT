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


class XrayError(RuntimeError):
    """Base exception for Xray related errors."""


class XrayRestartError(XrayError):
    """Raised when the Xray service failed to restart."""


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
    try:
        subprocess.run(["systemctl", "restart", XRAY_SERVICE], check=True)
    except subprocess.CalledProcessError as exc:
        logger.exception("Failed to restart Xray service", extra={"service": XRAY_SERVICE})
        raise XrayRestartError("xray_restart_failed") from exc


def _get_vless_inbound(cfg: dict) -> dict:
    for inbound in cfg.get("inbounds", []):
        if inbound.get("protocol") == "vless":
            return inbound
    logger.error("VLESS inbound not found in Xray configuration")
    raise XrayError("vless_inbound_not_found")


def _deduplicate_clients(clients: list[dict]) -> list[dict]:
    deduped: list[dict] = []
    seen: set[str] = set()
    for client in clients:
        cid = client.get("id")
        if cid and cid in seen:
            logger.warning("Removed duplicate client id from Xray config", extra={"client_id": cid})
            continue
        if cid:
            seen.add(cid)
        deduped.append(client)
    return deduped


def add_client(email: str, client_id: str | None = None) -> dict:
    """Backward compatible helper kept for legacy callers."""

    uuid_value = client_id or str(uuid4())
    add_client_no_duplicates(uuid_value, email)
    return {"uuid": uuid_value}


def add_client_no_duplicates(uuid_value: str, email: str) -> bool:
    cfg = _load()
    inbound = _get_vless_inbound(cfg)
    settings = inbound.setdefault("settings", {})
    clients = settings.setdefault("clients", [])

    clients[:] = _deduplicate_clients(clients)
    for client in clients:
        if client.get("id") == uuid_value:
            logger.info("Client already present in Xray config", extra={"uuid": uuid_value})
            return False

    clients.append({"id": uuid_value, "level": 0, "email": email})
    _save(cfg)
    _restart()
    logger.info("Added Xray client", extra={"uuid": uuid_value, "email": email})
    return True


def remove_client(uuid_value: str) -> bool:
    cfg = _load()
    inbound = _get_vless_inbound(cfg)
    settings = inbound.setdefault("settings", {})
    clients = settings.setdefault("clients", [])
    before = len(clients)
    settings["clients"] = [client for client in clients if client.get("id") != uuid_value]
    _save(cfg)
    _restart()
    removed = len(settings["clients"]) < before
    if removed:
        logger.info("Removed Xray client", extra={"uuid": uuid_value})
    else:
        logger.warning("Attempted to remove unknown Xray client", extra={"uuid": uuid_value})
    return removed
