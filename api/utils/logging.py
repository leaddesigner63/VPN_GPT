"""Central logging configuration for the VPN_GPT backend."""
from __future__ import annotations

import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Optional

_DEFAULT_LOG_LEVEL = os.getenv("VPN_GPT_LOG_LEVEL", "INFO").upper()
_DEFAULT_LOG_DIR = Path(
    os.getenv("VPN_GPT_LOG_DIR", Path(__file__).resolve().parents[2] / "logs")
)
_DEFAULT_LOG_DIR.mkdir(parents=True, exist_ok=True)
_DEFAULT_LOG_FILE = _DEFAULT_LOG_DIR / os.getenv("VPN_GPT_LOG_FILE", "api.log")

_DEFAULT_FORMAT = (
    "%(asctime)s | %(levelname)s | %(name)s:%(lineno)d | %(message)s"
)


def _build_file_handler() -> RotatingFileHandler:
    handler = RotatingFileHandler(
        _DEFAULT_LOG_FILE,
        maxBytes=int(os.getenv("VPN_GPT_LOG_MAX_BYTES", 5 * 1024 * 1024)),
        backupCount=int(os.getenv("VPN_GPT_LOG_BACKUP_COUNT", 5)),
        encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter(_DEFAULT_FORMAT))
    return handler


def _build_stream_handler() -> logging.StreamHandler:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter(_DEFAULT_FORMAT))
    return handler


def configure_logging(level: Optional[str] = None) -> None:
    """Initialise logging handlers once for the whole application."""

    root = logging.getLogger("vpn_gpt")
    if root.handlers:
        # Logging already configured (for example in tests or reloads).
        return

    root.setLevel(level or _DEFAULT_LOG_LEVEL)
    root.addHandler(_build_file_handler())
    root.addHandler(_build_stream_handler())
    root.propagate = False


def get_logger(name: str) -> logging.Logger:
    """Return a module-level logger attached to the project root."""

    configure_logging()
    return logging.getLogger("vpn_gpt").getChild(name)


__all__ = ["configure_logging", "get_logger"]
