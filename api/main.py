"""FastAPI application setup for the VPN_GPT backend."""
from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.openapi.utils import get_openapi
from fastapi.responses import JSONResponse
from dotenv import load_dotenv

load_dotenv()

app = FastAPI(title="VPN_GPT Action Hub", version="1.0.0")

from api.endpoints.vpn import router as vpn_router  # noqa: E402
from api.endpoints.users import router as users_router  # noqa: E402
from api.endpoints.morune import router as morune_router  # noqa: E402
from api.endpoints.notify import router as notify_router  # noqa: E402
from api.endpoints.admin import router as admin_router  # noqa: E402

app.include_router(vpn_router, prefix="/vpn", tags=["vpn"])
app.include_router(users_router, prefix="/users", tags=["users"])
app.include_router(morune_router, prefix="/morune", tags=["morune"])
app.include_router(notify_router, prefix="/notify", tags=["notify"])
app.include_router(admin_router, prefix="/admin", tags=["admin"])


@app.get("/healthz")
async def healthcheck() -> dict[str, bool]:
    """Simple health-check endpoint used for readiness probes."""
    return {"ok": True}


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:  # noqa: ARG001
    """Return a uniform JSON payload for unexpected exceptions."""
    return JSONResponse(status_code=500, content={"ok": False, "error": str(exc)})


def custom_openapi() -> dict:
    """Attach server metadata to the generated OpenAPI schema."""
    if app.openapi_schema:
        return app.openapi_schema

    openapi_schema = get_openapi(
        title="VPN_GPT Action API",
        version="1.0.0",
        description="API for GPT Actions — manage VPN keys, users, and payments",
        routes=app.routes,
    )
    openapi_schema["servers"] = [{"url": "http://45.92.174.166:8080"}]
    app.openapi_schema = openapi_schema
    return app.openapi_schema


app.openapi = custom_openapi

__all__ = ["app"]
