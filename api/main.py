"""Main FastAPI application for VPN_GPT."""
from __future__ import annotations

import os
from importlib import resources
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.utils import get_openapi
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field

from api.utils.logging import configure_logging, get_logger
from api.config import (
    BOT_PAYMENT_URL,
    EXPIRED_KEY_POLL_SECONDS,
    RENEWAL_NOTIFICATION_POLL_SECONDS,
)

# === Initialization ===
load_dotenv()
configure_logging()
logger = get_logger("api")

app = FastAPI(title="VPN_GPT Action Hub", version="1.0.0")


def _extract_origin(url: str) -> str | None:
    parsed = urlparse(url)
    if parsed.scheme and parsed.netloc:
        return f"{parsed.scheme}://{parsed.netloc}"
    return None


cors_env = os.getenv("CORS_ALLOW_ORIGINS")
if cors_env:
    origins = [origin.strip() for origin in cors_env.split(",") if origin.strip()]
else:
    default_origin = _extract_origin(BOT_PAYMENT_URL)
    origins = [default_origin] if default_origin else []

if origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["*"],
        allow_credentials=False,
    )

# === Routers ===
from api.endpoints import admin, morune, notify, payments, referrals, stars, users, vpn  # noqa: E402
from api.utils import db  # noqa: E402
from api.utils.expired_keys import ExpiredKeyMonitor  # noqa: E402
from api.utils.notifications import RenewalNotificationScheduler  # noqa: E402


expired_key_monitor = ExpiredKeyMonitor(interval_seconds=EXPIRED_KEY_POLL_SECONDS)
renewal_notification_scheduler = RenewalNotificationScheduler(
    interval_seconds=RENEWAL_NOTIFICATION_POLL_SECONDS
)


class RootResponse(BaseModel):
    """Schema describing the payload returned by the API root endpoints."""

    ok: bool = Field(..., description="Indicates whether the service is operating normally.")
    message: str = Field(..., description="Short description of the service state.")
    docs_url: str = Field(..., description="Relative URL of the interactive API documentation.")
    openapi_url: str = Field(..., description="Relative URL of the OpenAPI specification document.")


class HealthResponse(BaseModel):
    """Schema describing the payload returned by the health-check endpoint."""

    ok: bool = Field(..., description="Indicates whether the service is operating normally.")


@app.on_event("startup")
def ensure_database() -> None:
    """Initialise the SQLite database schema if it does not exist."""
    logger.info("Initialising database schema if required")
    db.init_db()
    db.auto_update_missing_fields()
    logger.info("Database initialisation complete")
    expired_key_monitor.start()
    renewal_notification_scheduler.start()


# === Router registration ===
app.include_router(vpn.router)
app.include_router(users.router)
app.include_router(payments.router)
app.include_router(morune.router)
app.include_router(morune.legacy_router)
app.include_router(referrals.router)
app.include_router(notify.router, prefix="/notify", tags=["notify"])
app.include_router(admin.router, prefix="/admin", tags=["admin"])
app.include_router(stars.router)


_SITE_ADMIN_PAGE = Path(__file__).resolve().parents[2] / "site" / "admin.html"


def _load_admin_panel_html() -> str:
    """Return the pre-built admin panel HTML."""

    if _SITE_ADMIN_PAGE.exists():
        return _SITE_ADMIN_PAGE.read_text(encoding="utf-8")

    html_path = resources.files("api.admin_panel").joinpath("admin_panel.html")
    return html_path.read_text(encoding="utf-8")


def _root_payload() -> RootResponse:
    """Return a consistent payload for root endpoints."""

    return RootResponse(
        ok=True,
        message="VPN_GPT Action API is running.",
        docs_url="/docs",
        openapi_url="/openapi.json",
    )


@app.get("/", response_model=RootResponse, include_in_schema=False)
def root() -> RootResponse:
    """Provide a friendly message at the API root."""

    return _root_payload()


@app.get("/api/", response_model=RootResponse, include_in_schema=False)
def api_root() -> RootResponse:
    """Provide a friendly message at the /api/ path for legacy clients."""

    return _root_payload()


@app.get("/admin/ui", include_in_schema=False, response_class=HTMLResponse)
def serve_admin_panel() -> HTMLResponse:
    """Serve the interactive web admin panel."""

    return HTMLResponse(content=_load_admin_panel_html())


@app.on_event("shutdown")
def stop_background_tasks() -> None:
    """Ensure background monitors are stopped when the application shuts down."""

    logger.info("Stopping expired key monitor")
    expired_key_monitor.stop()
    logger.info("Stopping renewal notification scheduler")
    renewal_notification_scheduler.stop()


# === Health check ===
@app.get("/healthz", response_model=HealthResponse)
def healthz() -> HealthResponse:
    """Simple health-check endpoint used for monitoring."""
    logger.debug("Health check endpoint called")
    return HealthResponse(ok=True)


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
        openapi_schema["servers"] = [
            {"url": server_url, "description": "Production deployment"}
        ]
        logger.info("Configured OpenAPI server override: %s", server_url)

    app.openapi_schema = openapi_schema
    return app.openapi_schema


app.openapi = custom_openapi
__all__ = ["app"]
