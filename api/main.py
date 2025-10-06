import os
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from dotenv import load_dotenv

load_dotenv()

app = FastAPI(title="VPN_GPT Action Hub", version="1.0.0")

# Routers
from endpoints.vpn import router as vpn_router
from endpoints.users import router as users_router
from endpoints.morune import router as morune_router
from endpoints.notify import router as notify_router
from endpoints.admin import router as admin_router

app.include_router(vpn_router, prefix="/vpn", tags=["vpn"])
app.include_router(users_router, prefix="/users", tags=["users"])
app.include_router(morune_router, prefix="/morune", tags=["morune"])
app.include_router(notify_router, prefix="/notify", tags=["notify"])
app.include_router(admin_router, prefix="/admin", tags=["admin"])

# simple healthcheck
@app.get("/healthz")
def healthz():
    return {"ok": True}

# uniform error handler
@app.exception_handler(Exception)
async def all_exception_handler(request: Request, exc: Exception):
    return JSONResponse(status_code=500, content={"ok": False, "error": str(exc)})

