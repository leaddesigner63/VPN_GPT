import datetime
import sqlite3

from fastapi import APIRouter

from api.utils.logging import get_logger

router = APIRouter()
logger = get_logger("endpoints.expiring")

@router.get("/users/expiring")
async def list_expiring_users():
    """Возвращает пользователей, у которых срок действия VPN истекает в ближайшие 3 дня."""
    conn = sqlite3.connect("/root/VPN_GPT/dialogs.db")
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS vpn_keys (
            username TEXT,
            uuid TEXT,
            expires TEXT,
            active INTEGER DEFAULT 1
        )
    """)
    today = datetime.date.today()
    limit = (today + datetime.timedelta(days=3)).strftime("%Y-%m-%d")
    cur.execute("SELECT username, uuid, expires FROM vpn_keys WHERE expires <= ? AND active=1", (limit,))
    rows = cur.fetchall()
    conn.close()
    result = [{"username": u, "uuid": x, "expires": e} for u, x, e in rows]
    logger.info("Found %d expiring users", len(result))
    return {"ok": True, "expiring": result}
