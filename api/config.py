"""Configuration helpers for the FastAPI service."""
from __future__ import annotations

from dotenv import load_dotenv

from .utils.env import get_vless_host

# Ensure variables from the project-level `.env` are available via ``os.getenv``
# before computing derived configuration values.
load_dotenv()

# Export the resolved domain so that other modules can simply import it from
# ``api.config`` without duplicating environment parsing logic. The
# ``get_vless_host`` helper already knows how to pull the value from both
# process environment variables and the `.env` file, applying normalisation and
# sensible fallbacks when necessary.
DOMAIN = get_vless_host()

__all__ = ["DOMAIN"]
