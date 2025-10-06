import os
from datetime import datetime, timedelta
from fastapi import APIRouter, HTTPException, Header
from pydantic import BaseModel, Field
from api.utils import db
from api.utils.xray import add_client, remove_client

router = APIRouter()

ADMIN_TOKEN = os.getenv("ADMIN_TOKEN")

def require_admin(x_admin_token: str | None):
    if not x_admin_token or x_admin_token != ADMIN_TOKEN:
        raise HTTPException(status_code=401, detail="Unauthorized")

class IssueKeyIn(BaseModel):
    user_id: str
    username: str | None = None
    days: int = Field(30, ge=1, le=365)

@router.post("/issue_key")
def issue_key(payload: IssueKeyIn, x_admin_token: str | None = Header(default=None)):
    require_admin(x_admin_token)
    email = f"user_{payload.user_id}@auto"
    x = add_client(email=email)
    uuid = x["uuid"]
    issued = datetime.utcnow()
    expires = issued + timedelta(days=payload.days)
    link = f"vless://{uuid}@your-host:2053?security=reality#VPN_GPT"  # при желании сформируй полностью
    with db.connect() as con:
        con.execute("""
        INSERT INTO vpn_keys (user_id, username, uuid, link, issued_at, expires_at, active)
        VALUES (?, ?, ?, ?, ?, ?, 1)
        """, (payload.user_id, payload.username, uuid, link, issued.isoformat(), expires.isoformat()))
    return {"ok": True, "uuid": uuid, "link": link, "expires_at": expires.isoformat()}

class RenewIn(BaseModel):
    uuid: str
    days: int = Field(30, ge=1, le=365)

@router.post("/renew_key")
def renew_key(payload: RenewIn, x_admin_token: str | None = Header(default=None)):
    require_admin(x_admin_token)
    with db.connect() as con:
        cur = con.execute("SELECT * FROM vpn_keys WHERE uuid=? AND active=1", (payload.uuid,))
        row = cur.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Active key not found")
        prev_exp = datetime.fromisoformat(row["expires_at"])
        new_exp = max(prev_exp, datetime.utcnow()) + timedelta(days=payload.days)
        con.execute("UPDATE vpn_keys SET expires_at=? WHERE uuid=?", (new_exp.isoformat(), payload.uuid))
    return {"ok": True, "uuid": payload.uuid, "new_expires_at": new_exp.isoformat()}

class DisableIn(BaseModel):
    uuid: str

@router.post("/disable_key")
def disable_key(payload: DisableIn, x_admin_token: str | None = Header(default=None)):
    require_admin(x_admin_token)
    removed = remove_client(payload.uuid)
    db.mark_disabled(payload.uuid)
    return {"ok": True, "removed_from_xray": removed}

