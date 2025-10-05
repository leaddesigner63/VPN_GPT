from __future__ import annotations
import os
from typing import Callable, Optional, TypeVar

try:
    from dotenv import load_dotenv  # type: ignore
except ModuleNotFoundError:  # pragma: no cover - optional dependency
    def load_dotenv(*_args, **_kwargs):  # type: ignore
        return False


load_dotenv()

T = TypeVar("T")


def _read_env(
    name: str,
    *,
    cast: Optional[Callable[[str], T]] = None,
    default: Optional[T] = None,
) -> Optional[T]:
    value = os.getenv(name)
    if value is None or value == "":
        return default
    if cast is None:
        return value  # type: ignore[return-value]
    try:
        return cast(value)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"Environment variable {name} has invalid value") from exc


BOT_TOKEN = _read_env("BOT_TOKEN")
GPT_API_KEY = _read_env("GPT_API_KEY")
GPT_ASSISTANT_ID = _read_env("GPT_ASSISTANT_ID")
ADMIN_ID = _read_env("ADMIN_ID", cast=int)


