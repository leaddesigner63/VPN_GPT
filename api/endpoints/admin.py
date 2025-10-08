from __future__ import annotations




from api.utils.vless import build_vless_link
import os
import shutil
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Header, HTTPException

from api.utils import db

router = APIRouter()
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN")
DB_PATH = Path(db.DB_PATH)

def require_admin(x_admin_token: str | None):
    if not x_admin_token or x_admin_token != ADMIN_TOKEN:
        raise HTTPException(status_code=401, detail="Unauthorized")

@router.post("/backup_db")
def backup_db(x_admin_token: str | None = Header(default=None)):
    require_admin(x_admin_token)
    ts = datetime.utcnow().strftime("%Y%m%d-%H%M%S")
    backup_dir = Path(os.getenv("BACKUP_DIR", DB_PATH.parent))
    backup_dir.mkdir(parents=True, exist_ok=True)
    dest = backup_dir / f"db-backup-{ts}.sqlite3"
    shutil.copyfile(DB_PATH, dest)
    return {"ok": True, "backup": str(dest)}

