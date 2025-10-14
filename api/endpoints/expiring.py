from typing import Any

from fastapi import APIRouter, Query

from api.utils import db
from api.utils.logging import get_logger

router = APIRouter()
logger = get_logger("endpoints.expiring")

@router.get("/users/expiring")
async def list_expiring_users(days: int = Query(default=3, ge=1, le=365)) -> dict[str, Any]:
    """Возвращает пользователей, у которых срок действия VPN истекает в ближайшие ``days`` дней."""
    records = db.list_expiring_keys(within_days=days)
    logger.info("Found expiring users", extra={"count": len(records), "days": days})
    return {"ok": True, "expiring": records}
