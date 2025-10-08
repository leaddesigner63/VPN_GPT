from __future__ import annotations




import os
from dotenv import load_dotenv
ENV_PATH = "/root/VPN_GPT/.env"
if not os.path.exists(ENV_PATH):
    raise RuntimeError(f".env не найден по пути {ENV_PATH}")
load_dotenv(ENV_PATH, override=True)
def require_env(name: str) -> str:
    v = os.getenv(name)
    if not v:
        raise RuntimeError(f"Переменная окружения {name} не задана в {ENV_PATH}")
    return v
VLESS_HOST = require_env("VLESS_HOST").strip()
VLESS_PORT = int(require_env("VLESS_PORT"))
print(f"[CONFIG] VLESS_HOST={VLESS_HOST}, VLESS_PORT={VLESS_PORT}")
