import os
import shutil
from datetime import datetime
from fastapi import APIRouter, Header, HTTPException

router = APIRouter()
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN")
DB_PATH = os.getenv("DATABASE", "/root/VPN_GPT/dialogs.db")

def require_admin(x_admin_token: str | None):
    if not x_admin_token or x_admin_token != ADMIN_TOKEN:
        raise HTTPException(status_code=401, detail="Unauthorized")

@router.post("/backup_db")
def backup_db(x_admin_token: str | None = Header(default=None)):
    require_admin(x_admin_token)
    ts = datetime.utcnow().strftime("%Y%m%d-%H%M%S")
    dest = f"/root/VPN_GPT/db-backup-{ts}.sqlite3"
    shutil.copyfile(DB_PATH, dest)
    return {"ok": True, "backup": dest}

