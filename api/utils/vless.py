from __future__ import annotations




from api.config import VLESS_HOST, VLESS_PORT
def build_vless_link(uuid: str, username: str) -> str:
    return f"vless://{uuid}@{VLESS_HOST}:{VLESS_PORT}?encryption=none#{username}"
