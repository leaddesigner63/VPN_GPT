"""Main FastAPI application for VPN_GPT."""
from __future__ import annotations

import os
from typing import Any
from urllib.parse import urlparse

from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.utils import get_openapi
from fastapi.responses import JSONResponse

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
from api.endpoints import admin, morune, notify, payments, referrals, users, vpn  # noqa: E402
from api.utils import db  # noqa: E402
from api.utils.expired_keys import ExpiredKeyMonitor  # noqa: E402
from api.utils.notifications import RenewalNotificationScheduler  # noqa: E402


expired_key_monitor = ExpiredKeyMonitor(interval_seconds=EXPIRED_KEY_POLL_SECONDS)
renewal_notification_scheduler = RenewalNotificationScheduler(
    interval_seconds=RENEWAL_NOTIFICATION_POLL_SECONDS
)


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


@app.on_event("shutdown")
def stop_background_tasks() -> None:
    """Ensure background monitors are stopped when the application shuts down."""

    logger.info("Stopping expired key monitor")
    expired_key_monitor.stop()
    logger.info("Stopping renewal notification scheduler")
    renewal_notification_scheduler.stop()


# === Health check ===
@app.get("/healthz")
def healthz() -> dict[str, bool]:
    """Simple health-check endpoint used for monitoring."""
    logger.debug("Health check endpoint called")
    return {"ok": True}


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

    app.openapi_schema = openapi_schema
    return app.openapi_schema


app.openapi = custom_openapi
__all__ = ["app"]
