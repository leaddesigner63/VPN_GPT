"""System status endpoints."""

from __future__ import annotations

import os
import shutil
import subprocess

from fastapi import APIRouter, Depends

from api.utils import db
from api.utils.auth import require_admin
from api.utils.logging import get_logger

router = APIRouter()

logger = get_logger("endpoints.status")


def _systemctl_status(service: str) -> str:
    """Return the normalised status of a systemd service."""

    systemctl = shutil.which("systemctl")
    if not systemctl:
        logger.warning("systemctl binary not found; reporting degraded status", extra={"service": service})
        return "degraded"

    try:
        result = subprocess.run(
            [systemctl, "is-active", service],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        logger.warning("systemctl check failed", extra={"service": service, "error": str(exc)})
        return "degraded"

    status = (result.stdout or "").strip().lower()
    if result.returncode == 0 and status == "active":
        return "active"

    if status in {"inactive", "failed", "unknown"}:
        return "offline"

    return "degraded"


def _bot_status() -> str:
    token = os.getenv("BOT_TOKEN")
    if not token:
        logger.warning("BOT_TOKEN is not configured; bot status offline")
        return "offline"

    return "active"


@router.get("/status")
def system_status(_: None = Depends(require_admin)) -> dict[str, object]:
    """Return a high-level summary of system state."""

    xray_status = _systemctl_status("xray")
    bot_status = _bot_status()
    vpn_keys_total = db.count_vpn_keys(active=True)
    expiring = db.count_expiring_users(days=3, active=True)

    return {
        "ok": True,
        "xray_status": xray_status,
        "bot_status": bot_status,
        "vpn_keys": vpn_keys_total,
        "expiring": expiring,
    }

