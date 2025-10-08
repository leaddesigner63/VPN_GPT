"""Utilities for reading configuration from environment files."""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Iterable

from dotenv import dotenv_values


def _candidate_keys() -> Iterable[str]:
    """Return the environment keys that may contain the VLESS host."""

    # Order matters: prefer the explicit key first and fall back to common
    # alternatives that may exist in legacy deployments.
    return ("VLESS_HOST", "VLESS_DOMAIN", "DOMAIN", "DOMAIN_NAME", "HOST")


def _normalise_host(value: str | None) -> str | None:
    """Clean up host values and drop placeholders.

    The production `.env` keeps only the bare domain name. Nevertheless, we
    guard against accidental schemes, trailing slashes, or placeholders used in
    development to ensure a consistent result.
    """

    if not value:
        return None

    candidate = value.strip()
    if not candidate:
        return None

    # Drop URL schemes if somebody provided `https://example.com`.
    if "://" in candidate:
        candidate = candidate.split("://", 1)[1]

    candidate = candidate.strip("/")
    if not candidate:
        return None

    # Ignore obvious placeholders left from local development configs.
    placeholders = {"your_host", "example.com", "localhost"}
    if candidate.lower() in placeholders:
        return None

    return candidate


@lru_cache(maxsize=1)
def get_vless_host(default: str = "vpn-gpt.store") -> str:
    """Return the VLESS host using environment configuration.

    The production server keeps the domain in the project root `.env`. When the
    environment variable is missing (for example after deployments), we read
    the file directly so that freshly issued VLESS links contain the real
    domain instead of a placeholder.
    """

    host = _normalise_host(os.getenv("VLESS_HOST"))
    if host:
        return host

    env_path = Path(__file__).resolve().parents[2] / ".env"
    if env_path.exists():
        values = dotenv_values(env_path)
        for key in _candidate_keys():
            host = _normalise_host(values.get(key))
            if host:
                return host

    return default


__all__ = ["get_vless_host"]

