from __future__ import annotations




from api.config import VLESS_HOST, VLESS_PORT
from api.utils.logging import get_logger

logger = get_logger("utils.vless")


def build_vless_link(uuid: str, username: str) -> str:
    link = f"vless://{uuid}@{VLESS_HOST}:{VLESS_PORT}?encryption=none#{username}"
    logger.info("Constructed VLESS link", extra={"uuid": uuid, "username": username})
    return link
