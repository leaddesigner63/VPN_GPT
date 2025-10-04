import json, os, uuid
from config import XRAY_CONFIG, SERVER_IP

def add_vpn_user():
    user_id = str(uuid.uuid4())
    with open(XRAY_CONFIG) as f:
        data = json.load(f)
    client = {"id": user_id, "level": 0, "email": f"user_{user_id[:6]}@auto"}
    data["inbounds"][0]["settings"]["clients"].append(client)
    with open(XRAY_CONFIG, "w") as f:
        json.dump(data, f, indent=2)
    os.system("systemctl restart xray")
    link = f"vless://{user_id}@{SERVER_IP}:2053?encryption=none&security=none&type=tcp&headerType=none#BusinessVPN"
    return link

