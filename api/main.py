"""Main FastAPI application for VPN_GPT."""
from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any
from dotenv import load_dotenv
from fastapi import APIRouter, FastAPI, Request
from fastapi.openapi.utils import get_openapi
from fastapi.responses import JSONResponse

from api.utils.logging import configure_logging, get_logger

# === Initialization ===
load_dotenv()
configure_logging()
logger = get_logger("api")

DEFAULT_OPENAPI_SERVER = "https://vpn-gpt.store/api"

app = FastAPI(title="VPN_GPT Action Hub", version="1.0.0")


def _normalise_prefix(value: str | None) -> str:
    """Ensure router prefixes always have a leading slash and no trailing slash."""

    if not value:
        return ""

    value = value.strip()
    if not value:
        return ""

    if not value.startswith("/"):
        value = "/" + value

    # ``/`` should behave the same as an empty prefix.
    if value == "/":
        return ""

    return value.rstrip("/")


def _compose_prefix(base: str | None, extra: str | None) -> str:
    """Join two router prefixes without introducing double slashes."""

    base_norm = _normalise_prefix(base)
    extra_norm = _normalise_prefix(extra)

    if not base_norm:
        return extra_norm
    if not extra_norm:
        return base_norm
    return f"{base_norm}{extra_norm}"


SECONDARY_API_PREFIX = _normalise_prefix(os.getenv("API_PREFIX", "/api"))


def _register_router(router: APIRouter, *, prefix: str | None = None, **kwargs: Any) -> None:
    """Register ``router`` on the base path and (optionally) under ``API_PREFIX``.

    The production deployment serves the application behind a reverse proxy that
    forwards requests prefixed with ``/api``.  Historically our routes were
    mounted directly on paths such as ``/vpn`` which meant proxied requests like
    ``/api/vpn/issue_key`` returned a 404.  To maintain backwards compatibility
    we now mount every router twice: once on its original prefix and again with
    the additional API prefix.  Duplicate prefixes are ignored to avoid
    registering the same router twice under the same path.
    """

    primary_prefix = _normalise_prefix(prefix)
    prefixes = {primary_prefix}

    if SECONDARY_API_PREFIX:
        combined_prefix = _compose_prefix(SECONDARY_API_PREFIX, primary_prefix)
        if combined_prefix not in prefixes:
            prefixes.add(combined_prefix)

    for resolved_prefix in prefixes:
        app.include_router(router, prefix=resolved_prefix, **kwargs)

# === Routers ===
from api.endpoints import admin, notify, status, users, vpn  # noqa: E402
from api.utils import db  # noqa: E402


@app.on_event("startup")
def ensure_database() -> None:
    """Initialise the SQLite database schema if it does not exist."""
    logger.info("Initialising database schema if required")
    db.init_db()
    db.auto_update_missing_fields()
    logger.info("Database initialisation complete")


# === Router registration ===
_register_router(vpn.router, prefix="/vpn", tags=["vpn"])
_register_router(users.router)
_register_router(notify.router)
_register_router(admin.router, prefix="/admin", tags=["admin"])
_register_router(status.router, tags=["health"])


# === Health check ===
@app.get("/healthz")
def healthz() -> dict[str, Any]:
    """Simple health-check endpoint used for monitoring."""
    logger.debug("Health check endpoint called")
    return {
        "ok": True,
        "service": "api",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "message": "Service is healthy.",
    }


if SECONDARY_API_PREFIX:
    app.add_api_route(
        f"{SECONDARY_API_PREFIX}/healthz",
        healthz,
        methods=["GET"],
        name="healthz-proxied",
    )


# === Global error handler ===
@app.exception_handler(Exception)
async def handle_unexpected_error(request: Request, exc: Exception) -> JSONResponse:
    """Return a uniform JSON error response for any unhandled exception."""
    _ = request  # FastAPI requires this argument
    logger.exception("Unhandled exception during request processing: %s", exc)
    return JSONResponse(status_code=500, content={"ok": False, "error": str(exc)})


# === Custom OpenAPI ===
def custom_openapi() -> dict[str, Any]:
    """Attach metadata and optionally configure the server URL for Swagger UI."""
    if app.openapi_schema:
        return app.openapi_schema

    openapi_schema = get_openapi(
        title="VPN_GPT Action API",
        version="1.0.0",
        description="API for GPT Actions — manage VPN keys, users, and notifications",
        routes=app.routes,
    )

    server_url = os.getenv("OPENAPI_SERVER_URL")
    if server_url:
        openapi_schema["servers"] = [{"url": server_url}]
        logger.info("Configured OpenAPI server override: %s", server_url)
    else:
        openapi_schema["servers"] = [{"url": DEFAULT_OPENAPI_SERVER}]
        logger.info(
            "Using default OpenAPI server URL", extra={"server_url": DEFAULT_OPENAPI_SERVER}
        )

    app.openapi_schema = openapi_schema
    return app.openapi_schema


app.openapi = custom_openapi
__all__ = ["app"]
