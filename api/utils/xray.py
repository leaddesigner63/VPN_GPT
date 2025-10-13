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
    seen_ids: set[str] = set()
    seen_emails: set[str] = set()
    for client in reversed(clients):
        cid = client.get("id")
        email = client.get("email")
        if cid and cid in seen_ids:
            logger.warning("Removed duplicate client id from Xray config", extra={"client_id": cid})
            continue
        if email and email in seen_emails:
            logger.warning("Removed duplicate client email from Xray config", extra={"email": email})
            continue
        if cid:
            seen_ids.add(cid)
        if email:
            seen_emails.add(email)
        deduped.append(client)
    deduped.reverse()
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

    deduped_clients = _deduplicate_clients(clients)
    dedup_performed = deduped_clients != clients
    clients[:] = deduped_clients

    config_changed = dedup_performed

    existing_by_email = next((client for client in clients if client.get("email") == email), None)
    if existing_by_email is not None:
        if existing_by_email.get("id") != uuid_value:
            logger.info(
                "Replacing Xray client id for email",
                extra={"old_uuid": existing_by_email.get("id"), "new_uuid": uuid_value, "email": email},
            )
            existing_by_email["id"] = uuid_value
            config_changed = True
        if existing_by_email.get("level") != 0:
            existing_by_email["level"] = 0
            config_changed = True
        if existing_by_email.get("email") != email:
            existing_by_email["email"] = email
            config_changed = True

        if config_changed:
            _save(cfg)
            _restart()
            if existing_by_email.get("id") == uuid_value and existing_by_email.get("email") == email:
                logger.info("Updated Xray client", extra={"uuid": uuid_value, "email": email})
            else:  # pragma: no cover - defensive branch
                logger.info("Normalised Xray client list")
            return True

        logger.info("Client already present in Xray config", extra={"uuid": uuid_value, "email": email})
        return False

    existing_by_id = next((client for client in clients if client.get("id") == uuid_value), None)
    if existing_by_id is not None:
        if existing_by_id.get("email") != email:
            existing_by_id["email"] = email
            config_changed = True
        if existing_by_id.get("level") != 0:
            existing_by_id["level"] = 0
            config_changed = True

        if config_changed:
            _save(cfg)
            _restart()
            logger.info("Updated Xray client", extra={"uuid": uuid_value, "email": email})
            return True

        logger.info("Client already present in Xray config", extra={"uuid": uuid_value, "email": email})
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
