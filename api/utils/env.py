"""Environment helpers for the API layer."""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Iterable

from dotenv import dotenv_values, find_dotenv


_CANDIDATE_KEYS: tuple[str, ...] = (
    "VLESS_HOST",
    "VLESS_DOMAIN",
    "DOMAIN",
    "DOMAIN_NAME",
    "HOST",
)


def _normalise_host(value: str | None) -> str | None:
    """Strip schemes and trailing slashes from host values."""

    if not value:
        return None

    host = value.strip()
    if not host:
        return None

    if "://" in host:
        host = host.split("://", 1)[1]

    # Remove everything after the first slash to handle accidental paths.
    host = host.split("/", 1)[0]

    return host or None


def _iter_env_files() -> Iterable[Path]:
    """Yield potential `.env` files in preference order."""

    seen: set[Path] = set()
    ordered: list[Path] = []

    def register(path: Path) -> None:
        if path not in seen and path.is_file():
            seen.add(path)
            ordered.append(path)

    override = os.getenv("ENV_FILE") or os.getenv("DOTENV_PATH")
    if override:
        register(Path(override).expanduser())

    project_root = os.getenv("PROJECT_ROOT")
    if project_root:
        register(Path(project_root).expanduser() / ".env")

    found = find_dotenv(usecwd=True)
    if found:
        register(Path(found))

    module_root = Path(__file__).resolve().parent
    for parent in (module_root, *module_root.parents):
        register(parent / ".env")

    cwd = Path.cwd()
    for parent in (cwd, *cwd.parents):
        register(parent / ".env")

    return ordered


@lru_cache(maxsize=1)
def _host_from_env_files() -> str | None:
    for env_file in _iter_env_files():
        try:
            values = dotenv_values(env_file)
        except OSError:
            continue
        if not values:
            continue
        for key in _CANDIDATE_KEYS:
            host = _normalise_host(values.get(key))
            if host:
                return host
    return None


def get_vless_host(default: str = "vpn-gpt.store") -> str:
    """Return the configured VLESS host or a sensible default."""

    for key in _CANDIDATE_KEYS:
        host = _normalise_host(os.getenv(key))
        if host:
            return host

    host = _host_from_env_files()
    if host:
        return host

    return default


__all__ = ["get_vless_host"]

