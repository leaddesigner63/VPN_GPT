from __future__ import annotations




import os
import shutil
from datetime import UTC, datetime
from pathlib import Path

from fastapi import APIRouter, Header, HTTPException

from api.utils import db
from api.utils.logging import get_logger

router = APIRouter()
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN")
DB_PATH = Path(db.DB_PATH)
logger = get_logger("endpoints.admin")

def require_admin(x_admin_token: str | None):
    if not x_admin_token or x_admin_token != ADMIN_TOKEN:
        logger.warning("Unauthorized admin request")
        raise HTTPException(status_code=401, detail="Unauthorized")
    logger.debug("Authorized admin request")

@router.post("/backup_db")
def backup_db(
    x_admin_token: str | None = Header(
        default=None,
        alias="X-Admin-Token",
        include_in_schema=False,
    )
):
    require_admin(x_admin_token)
    ts = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    backup_dir = Path(os.getenv("BACKUP_DIR", DB_PATH.parent))
    backup_dir.mkdir(parents=True, exist_ok=True)
    dest = backup_dir / f"db-backup-{ts}.sqlite3"
    shutil.copyfile(DB_PATH, dest)
    logger.info("Created database backup", extra={"destination": str(dest)})
    return {"ok": True, "backup": str(dest)}

