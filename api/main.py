"""Main FastAPI application for VPN_GPT."""
from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any

from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.openapi.utils import get_openapi
from fastapi.responses import JSONResponse

from api.utils.logging import configure_logging, get_logger

# === Initialization ===
load_dotenv()
configure_logging()
logger = get_logger("api")

DEFAULT_OPENAPI_BASE_URL = "https://vpn-gpt.store"
DEFAULT_API_ROOT_PATH = "/api"


def _normalise_root_path(raw_path: str | None) -> str:
    """Return a well-formed ASGI root path."""

    if not raw_path:
        return ""

    cleaned = raw_path.strip()
    if cleaned in {"", "/"}:
        return ""

    return "/" + cleaned.strip("/")


API_ROOT_PATH = _normalise_root_path(os.getenv("API_ROOT_PATH", DEFAULT_API_ROOT_PATH))


def _build_server_url(base_url: str, root_path: str) -> str:
    """Compose the full server URL, appending the root path when necessary."""

    base = base_url.rstrip("/")
    if not root_path:
        return base
    return f"{base}{root_path}"


DEFAULT_OPENAPI_SERVER = _build_server_url(DEFAULT_OPENAPI_BASE_URL, API_ROOT_PATH)

app = FastAPI(title="VPN_GPT Action Hub", version="1.0.0", root_path=API_ROOT_PATH)

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
app.include_router(vpn.router, prefix="/vpn", tags=["vpn"])
app.include_router(users.router)
app.include_router(notify.router)
app.include_router(admin.router, prefix="/admin", tags=["admin"])
app.include_router(status.router, tags=["health"])


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
