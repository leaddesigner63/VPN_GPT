"""Main FastAPI application for VPN_GPT."""
from __future__ import annotations

import os
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

app = FastAPI(title="VPN_GPT Action Hub", version="1.0.0")

# === Routers ===
from api.endpoints import admin, notify, users, vpn  # noqa: E402
from api.utils import db  # noqa: E402


@app.on_event("startup")
def ensure_database() -> None:
    """Initialise the SQLite database schema if it does not exist."""
    logger.info("Initialising database schema if required")
    db.init_db()
    logger.info("Database initialisation complete")


# === Router registration ===
app.include_router(vpn.router, prefix="/vpn", tags=["vpn"])
app.include_router(users.router)
app.include_router(notify.router, prefix="/notify", tags=["notify"])
app.include_router(admin.router, prefix="/admin", tags=["admin"])


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
