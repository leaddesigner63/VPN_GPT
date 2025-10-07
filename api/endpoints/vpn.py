from fastapi import APIRouter, Request
import sqlite3, json, os, uuid as uuidlib, subprocess, datetime

router = APIRouter()
DB = "/root/VPN_GPT/dialogs.db"
XRAY = "/usr/local/etc/xray/config.json"
HOST = os.getenv("VLESS_HOST", "vpn-gpt.store")
PORT = os.getenv("VLESS_PORT", "2053")

@router.post("/vpn/issue_key")
async def issue_vpn_key(request: Request):
    data = await request.json()
    username = data.get("username")
    days = int(data.get("days", 30))
    if not username:
        return {"ok": False, "error": "missing_username"}
    uid = str(uuidlib.uuid4())
    expires = (datetime.datetime.utcnow() + datetime.timedelta(days=days)).strftime("%Y-%m-%d")
    link = f"vless://{uid}@{HOST}:{PORT}?encryption=none#{username}"

    conn = sqlite3.connect(DB)
    cur = conn.cursor()
    cur.execute("INSERT INTO vpn_keys (username, uuid, active, expires) VALUES (?, ?, 1, ?)", (username, uid, expires))
    conn.commit(); conn.close()

    if os.path.exists(XRAY):
        cfg = json.load(open(XRAY))
        if "inbounds" in cfg:
            cfg["inbounds"][0]["settings"]["clients"].append({"id": uid, "email": username})
            json.dump(cfg, open(XRAY,"w"), indent=2)
        subprocess.run(["systemctl","restart","xray"], check=False)
    return {"ok": True, "link": link, "uuid": uid, "expires": expires}

@router.post("/vpn/renew_key")
async def renew_vpn_key(request: Request):
    data = await request.json()
    username = data.get("username")
    days = int(data.get("days", 30))
    if not username:
        return {"ok": False, "error": "missing_username"}
    conn = sqlite3.connect(DB)
    cur = conn.cursor()
    cur.execute("SELECT expires FROM vpn_keys WHERE username=? AND active=1 LIMIT 1",(username,))
    row = cur.fetchone()
    if not row: 
        return {"ok": False, "error": "user_not_found"}
    new_exp = (datetime.datetime.strptime(row[0], "%Y-%m-%d") + datetime.timedelta(days=days)).strftime("%Y-%m-%d")
    cur.execute("UPDATE vpn_keys SET expires=? WHERE username=?", (new_exp, username))
    conn.commit(); conn.close()
    return {"ok": True, "username": username, "expires": new_exp}

@router.post("/vpn/disable_key")
async def disable_vpn_key(request: Request):
    data = await request.json()
    uid = data.get("uuid")
    if not uid:
        return {"ok": False, "error": "missing_uuid"}
    conn = sqlite3.connect(DB)
    cur = conn.cursor()
    cur.execute("UPDATE vpn_keys SET active=0 WHERE uuid=?", (uid,))
    conn.commit(); conn.close()

    if os.path.exists(XRAY):
        cfg = json.load(open(XRAY))
        if "inbounds" in cfg:
            clients = cfg["inbounds"][0]["settings"]["clients"]
            cfg["inbounds"][0]["settings"]["clients"] = [c for c in clients if c.get("id") != uid]
            json.dump(cfg, open(XRAY,"w"), indent=2)
        subprocess.run(["systemctl","restart","xray"], check=False)
    return {"ok": True, "uuid": uid}
