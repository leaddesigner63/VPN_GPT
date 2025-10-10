from __future__ import annotations




import os

from dotenv import load_dotenv

from api.utils.logging import get_logger

ENV_PATH = "/root/VPN_GPT/.env"

logger = get_logger("config")

if not os.path.exists(ENV_PATH):
    logger.error(".env file is missing", extra={"path": ENV_PATH})
    raise RuntimeError(f".env не найден по пути {ENV_PATH}")

load_dotenv(ENV_PATH, override=True)


def require_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        logger.error("Required environment variable is missing", extra={"name": name})
        raise RuntimeError(f"Переменная окружения {name} не задана в {ENV_PATH}")
    logger.debug("Loaded environment variable", extra={"name": name})
    return value


VLESS_HOST = require_env("VLESS_HOST").strip()
VLESS_PORT = int(require_env("VLESS_PORT"))
logger.info("Configuration loaded", extra={"VLESS_HOST": VLESS_HOST, "VLESS_PORT": VLESS_PORT})
