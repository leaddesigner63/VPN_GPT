from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from uuid import uuid4

from api.utils.logging import get_logger


def _normalise_service_name(raw: str | None, default: str = "xray") -> str:
    """Return a clean systemd service name from an environment value."""

    if raw is None:
        return default

    # Strip comments (everything after '#') and extraneous whitespace.
    cleaned = raw.split("#", 1)[0].strip()
    if not cleaned:
        return default

    return cleaned


def _service_name_candidates(preferred: str, default: str = "xray") -> list[str]:
    """Return possible service names that can be used to restart Xray.

    The function expands common variations like ``xray`` vs ``xray.service``
    while keeping the order deterministic and removing duplicates.
    """

    candidates: list[str] = []

    def _add(name: str | None) -> None:
        if name and name not in candidates:
            candidates.append(name)

    _add(preferred)

    if preferred.endswith(".service"):
        base = preferred[: -len(".service")]
    else:
        base = preferred

    _add(base)
    _add(f"{base}.service")

    if default:
        _add(default)
        _add(f"{default}.service")

    return candidates


XRAY_CONFIG = Path(os.getenv("XRAY_CONFIG", "/usr/local/etc/xray/config.json"))
_XRAY_SERVICE_RAW = os.getenv("XRAY_SERVICE")
XRAY_SERVICE = _normalise_service_name(_XRAY_SERVICE_RAW)

logger = get_logger("xray")

if _XRAY_SERVICE_RAW is not None and XRAY_SERVICE != _XRAY_SERVICE_RAW.strip():
    logger.warning(
        "Normalised XRAY_SERVICE value", extra={"provided": _XRAY_SERVICE_RAW, "used": XRAY_SERVICE}
    )


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
    candidates = _service_name_candidates(XRAY_SERVICE)
    logger.info(
        "Restarting Xray service", extra={"preferred": XRAY_SERVICE, "candidates": candidates}
    )

    errors: list[dict] = []

    for service_name in candidates:
        try:
            subprocess.run(["systemctl", "restart", service_name], check=True)
        except subprocess.CalledProcessError as exc:
            logger.warning(
                "Failed to restart Xray service candidate",
                extra={"service": service_name, "returncode": exc.returncode},
            )
            errors.append({"service": service_name, "returncode": exc.returncode})
            continue
        else:
            if service_name != XRAY_SERVICE:
                logger.info(
                    "Restarted Xray service using fallback name", extra={"service": service_name}
                )
            return

    logger.exception(
        "Failed to restart Xray service after trying all candidates",
        extra={"candidates": candidates, "errors": errors},
    )
    raise XrayRestartError("xray_restart_failed")


def _iter_vless_inbounds(cfg: dict) -> list[dict]:
    """Return a list of VLESS inbounds in the supplied configuration."""

    inbounds: list[dict] = []
    for inbound in cfg.get("inbounds", []):
        protocol = inbound.get("protocol")
        if isinstance(protocol, str) and protocol.casefold() == "vless":
            inbounds.append(inbound)
    return inbounds


def _get_vless_inbound(cfg: dict) -> dict:
    for inbound in _iter_vless_inbounds(cfg):
        return inbound
    logger.error("VLESS inbound not found in Xray configuration")
    raise XrayError("vless_inbound_not_found")


def _normalise_email(email: str | None) -> str | None:
    if email is None:
        return None
    stripped = email.strip()
    return stripped or None


def _email_key(email: str | None) -> str | None:
    normalised = _normalise_email(email)
    if normalised is None:
        return None
    return normalised.casefold()


def _deduplicate_clients(clients: list[dict]) -> list[dict]:
    deduped: list[dict] = []
    seen_ids: set[str] = set()
    seen_emails: set[str] = set()
    for client in reversed(clients):
        cid = client.get("id")
        email = client.get("email")
        normalised_email = _normalise_email(email)
        email_key = normalised_email.casefold() if normalised_email is not None else None
        if cid and cid in seen_ids:
            logger.warning("Removed duplicate client id from Xray config", extra={"client_id": cid})
            continue
        if email_key and email_key in seen_emails:
            logger.warning(
                "Removed duplicate client email from Xray config",
                extra={"email": normalised_email},
            )
            continue
        if cid:
            seen_ids.add(cid)
        if email_key:
            seen_emails.add(email_key)
        if email != normalised_email:
            normalised_client = dict(client)
            normalised_client["email"] = normalised_email
            deduped.append(normalised_client)
        else:
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

    normalised_email = _normalise_email(email)
    email_key = normalised_email.casefold() if normalised_email is not None else None

    if normalised_email is None:
        logger.error("Attempted to add Xray client without email", extra={"uuid": uuid_value})
        raise XrayError("email_required")

    existing_by_email = None
    if email_key:
        existing_by_email = next(
            (client for client in clients if _email_key(client.get("email")) == email_key), None
        )
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
        if existing_by_email.get("email") != normalised_email:
            existing_by_email["email"] = normalised_email
            config_changed = True

        if config_changed:
            _save(cfg)
            _restart()
            if existing_by_email.get("id") == uuid_value and existing_by_email.get("email") == normalised_email:
                logger.info("Updated Xray client", extra={"uuid": uuid_value, "email": email})
            else:  # pragma: no cover - defensive branch
                logger.info("Normalised Xray client list")
            return True

        logger.info("Client already present in Xray config", extra={"uuid": uuid_value, "email": email})
        return False

    existing_by_id = next((client for client in clients if client.get("id") == uuid_value), None)
    if existing_by_id is not None:
        if existing_by_id.get("email") != normalised_email:
            existing_by_id["email"] = normalised_email
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

    clients.append({"id": uuid_value, "level": 0, "email": normalised_email})
    _save(cfg)
    _restart()
    logger.info("Added Xray client", extra={"uuid": uuid_value, "email": email})
    return True


def remove_client(uuid_value: str) -> bool:
    cfg = _load()
    removed = False

    for inbound in _iter_vless_inbounds(cfg):
        settings = inbound.get("settings")
        if not isinstance(settings, dict):
            logger.warning(
                "Unexpected settings container in Xray config",
                extra={"type": type(settings).__name__},
            )
            continue

        clients = settings.get("clients")
        if not isinstance(clients, list):
            logger.warning(
                "Unexpected clients container type in Xray config",
                extra={"type": type(clients).__name__},
            )
            continue

        filtered = [client for client in clients if client.get("id") != uuid_value]
        if len(filtered) < len(clients):
            settings["clients"] = filtered
            removed = True

    if removed:
        _save(cfg)
        _restart()
        logger.info("Removed Xray client", extra={"uuid": uuid_value})
        return True

    logger.warning("Attempted to remove unknown Xray client", extra={"uuid": uuid_value})
    return False
