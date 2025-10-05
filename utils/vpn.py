import uuid

def add_vpn_user():
    user_id = str(uuid.uuid4())[:8]
    return f"vless://{user_id}@45.92.174.166:2053?security=reality&fp=chrome#vpn_GPT-{user_id}"

