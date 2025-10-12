import asyncio
import logging
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Awaitable, Callable, Dict

import httpx
from aiogram import BaseMiddleware, Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    BufferedInputFile,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)
from dotenv import load_dotenv
from openai import OpenAI

from api.utils import db as api_db
from utils.qrgen import make_qr
from utils.limits import should_block_issue

# === Инициализация ===
load_dotenv("/root/VPN_GPT/.env")

BOT_TOKEN = os.getenv("BOT_TOKEN")
GPT_API_KEY = os.getenv("GPT_API_KEY")
ADMIN_ID = os.getenv("ADMIN_ID")
VPN_API_URL = os.getenv("VPN_API_URL", "https://vpn-gpt.store/api")
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "")

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
client = OpenAI(api_key=GPT_API_KEY)


class VPNAPIError(RuntimeError):
    """Wrapper for API errors returned by the VPN backend."""

    def __init__(self, code: str, *, status: int | None = None, details: dict | None = None):
        super().__init__(code)
        self.code = code
        self.status = status
        self.details = details or {}


@dataclass(slots=True)
class VPNKey:
    username: str
    uuid: str
    link: str
    expires_at: str


@dataclass(slots=True)
class RenewInfo:
    username: str
    expires_at: str


class VPNAPIClient:
    """Async wrapper around the FastAPI backend used by GPT and the bot."""

    def __init__(self, base_url: str, admin_token: str | None = None, *, timeout: float = 10.0):
        self.base_url = base_url.rstrip("/")
        self.admin_token = admin_token or None
        self._timeout = timeout

    def _headers(self) -> dict[str, str]:
        if not self.admin_token:
            return {}
        return {"X-Admin-Token": self.admin_token}

    async def _request(self, method: str, path: str, *, json: dict | None = None, params: dict | None = None) -> dict:
        url = f"{self.base_url}{path}"
        async with httpx.AsyncClient(timeout=self._timeout) as session:
            response = await session.request(method, url, json=json, params=params, headers=self._headers())

        status = response.status_code
        try:
            payload = response.json()
        except ValueError as exc:  # pragma: no cover - defensive
            logging.exception("VPN API вернул не-JSON", extra={"url": url, "status": status})
            raise VPNAPIError("invalid_json", status=status) from exc

        if status >= 400:
            error_code = payload.get("detail") if isinstance(payload, dict) else "http_error"
            raise VPNAPIError(str(error_code), status=status, details=payload if isinstance(payload, dict) else None)

        if isinstance(payload, dict) and payload.get("ok") is False:
            raise VPNAPIError(str(payload.get("error", "unknown_error")), status=status, details=payload)

        return payload

    async def issue_key(self, username: str, *, days: int = 3) -> VPNKey:
        payload = await self._request(
            "POST",
            "/vpn/issue_key",
            json={"username": username, "days": days},
        )
        return VPNKey(
            username=payload["username"],
            uuid=payload["uuid"],
            link=payload["link"],
            expires_at=payload["expires_at"],
        )

    async def renew_key(self, username: str, *, days: int = 30) -> RenewInfo:
        payload = await self._request(
            "POST",
            "/vpn/renew_key",
            json={"username": username, "days": days},
        )
        return RenewInfo(username=payload["username"], expires_at=payload["expires_at"])

    async def get_my_key(self, *, username: str | None = None, chat_id: int | None = None) -> dict:
        params: dict[str, Any] = {}
        if username:
            params["username"] = username
        if chat_id is not None:
            params["chat_id"] = chat_id
        return await self._request("GET", "/vpn/my_key", params=params)

    async def list_users(self) -> dict:
        return await self._request("GET", "/users/", params={"active_only": True})


vpn_api = VPNAPIClient(VPN_API_URL, admin_token=ADMIN_TOKEN or None)

DB_PATH = "/root/VPN_GPT/dialogs.db"

# === Логирование ===
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler("/root/VPN_GPT/bot.log"), logging.StreamHandler()],
)

logger = logging.getLogger(__name__)


def _load_issue_limit() -> tuple[int | None, str | None]:
    """Возвращает настроенный лимит выдачи ключей, если он задан."""

    for env_name in ("FREE_KEYS_LIMIT", "VPN_FREE_KEYS_LIMIT", "VPN_KEY_LIMIT"):
        raw_value = os.getenv(env_name)
        if raw_value is None or not raw_value.strip():
            continue

        try:
            limit_value = int(raw_value)
        except ValueError:
            logger.warning(
                "Игнорируем некорректное значение лимита", extra={"env": env_name, "value": raw_value}
            )
            continue

        if limit_value > 0:
            return limit_value, env_name

        logger.warning(
            "Лимит выдачи ключей должен быть положительным", extra={"env": env_name, "value": raw_value}
        )

    return None, None


KEY_ISSUE_LIMIT, KEY_LIMIT_ENV = _load_issue_limit()
if KEY_ISSUE_LIMIT:
    logger.info(
        "Включён лимит выдачи ключей", extra={"limit": KEY_ISSUE_LIMIT, "source": KEY_LIMIT_ENV}
    )


KEY_LIMIT_REACHED_MESSAGE = (
    "⚠️ Бесплатные демо-ключи закончились — мы уже выдали все доступные "
    "слоты. Подпишись на обновления, чтобы узнать о новых местах."
)
KEY_LIMIT_CHECK_FAILED_MESSAGE = (
    "⚠️ Сейчас не получается проверить доступность ключей. Попробуй позже."
)

# === Главное меню Telegram ===
main_kb = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="💡 Получить VPN")],
        [KeyboardButton(text="♻️ Продлить VPN")],
        [KeyboardButton(text="💬 Спросить")]
    ],
    resize_keyboard=True,
    one_time_keyboard=False
)

# === База данных ===
def ensure_tables() -> None:
    """Подготовка БД под требования API и бота."""

    try:
        api_db.init_db()
    except Exception:  # pragma: no cover - инициализация БД не критична для тестов
        logging.exception("Не удалось выполнить миграции API для БД")

    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS tg_users (
                username TEXT PRIMARY KEY,
                chat_id INTEGER,
                first_name TEXT,
                last_name TEXT,
                created_at TEXT
            )
            """
        )
        conn.commit()

def save_user(message: Message):
    username = message.from_user.username or f"id_{message.from_user.id}"
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            INSERT OR REPLACE INTO tg_users (username, chat_id, first_name, last_name, created_at)
            VALUES (?, ?, ?, ?, ?)
        """, (
            username,
            message.chat.id,
            message.from_user.first_name,
            message.from_user.last_name,
            datetime.now().isoformat()
        ))
        conn.commit()
    return username

# === Обработчики ===
async def issue_and_send_key(message: Message, username: str) -> None:
    await message.answer("⏳ Создаю тебе VPN-ключ…", reply_markup=main_kb)

    if KEY_ISSUE_LIMIT and KEY_ISSUE_LIMIT > 0:
        if not ADMIN_TOKEN:
            logger.error(
                "Включён лимит выдачи ключей, но ADMIN_TOKEN не задан", extra={"username": username}
            )
            await message.answer(KEY_LIMIT_CHECK_FAILED_MESSAGE, reply_markup=main_kb)
            return

        try:
            stats = await vpn_api.list_users()
        except VPNAPIError as api_error:
            logger.warning(
                "Не удалось проверить лимит выдачи ключей",
                extra={"username": username, "error": api_error.code, "status": api_error.status},
            )
            await message.answer(KEY_LIMIT_CHECK_FAILED_MESSAGE, reply_markup=main_kb)
            return
        except Exception:
            logger.exception("Сбой при проверке лимита выдачи ключей", extra={"username": username})
            await message.answer(KEY_LIMIT_CHECK_FAILED_MESSAGE, reply_markup=main_kb)
            return

        users_payload = stats.get("users") if isinstance(stats, dict) else None
        if not isinstance(users_payload, list):
            logger.warning("Некорректный ответ API при проверке лимита", extra={"payload": stats})
            await message.answer(KEY_LIMIT_CHECK_FAILED_MESSAGE, reply_markup=main_kb)
            return

        if should_block_issue(users_payload, username, KEY_ISSUE_LIMIT):
            logger.info(
                "Достигнут лимит выдачи ключей",
                extra={"limit": KEY_ISSUE_LIMIT, "username": username},
            )
            await message.answer(KEY_LIMIT_REACHED_MESSAGE, reply_markup=main_kb)
            return
    try:
        vpn_key = await vpn_api.issue_key(username)
    except VPNAPIError as api_error:
        logging.warning(
            "Не удалось выдать ключ", extra={"username": username, "error": api_error.code, "status": api_error.status}
        )
        error_code = (api_error.code or "").lower()
        if api_error.code in {"user_has_active_key", "duplicate"}:
            await message.answer(
                "ℹ️ У тебя уже есть активный VPN-ключ. Проверь предыдущие сообщения или продли текущий.",
                reply_markup=main_kb,
            )
        elif api_error.code == "invalid_days":
            await message.answer("⚠️ Некорректный срок действия ключа.", reply_markup=main_kb)
        elif "limit" in error_code or "quota" in error_code:
            await message.answer(KEY_LIMIT_REACHED_MESSAGE, reply_markup=main_kb)
        else:
            status_info = f" (код {api_error.status})" if api_error.status else ""
            await message.answer(
                "⚠️ Не получилось создать ключ. Попробуй ещё раз позже." + status_info,
                reply_markup=main_kb,
            )
        return
    except Exception:
        logging.exception("Сбой при выдаче VPN-ключа", extra={"username": username})
        await message.answer(
            "⚠️ Не получилось создать ключ. Попробуй ещё раз чуть позже.",
            reply_markup=main_kb,
        )
        return

    await message.answer(
        "🎁 Твой бесплатный VPN-ключ готов!\n\n"
        f"🔗 Ссылка:\n{vpn_key.link}\n"
        f"⏳ Действует до: {vpn_key.expires_at}",
        reply_markup=main_kb,
    )

    qr_stream = make_qr(vpn_key.link)
    await message.answer_photo(
        BufferedInputFile(qr_stream.getvalue(), filename="vpn_key.png"),
        caption="📱 Отсканируй QR-код для быстрого подключения",
    )


async def renew_vpn_key(message: Message, username: str) -> None:
    await message.answer("♻️ Продляю твой VPN…", reply_markup=main_kb)
    try:
        info = await vpn_api.renew_key(username)
    except VPNAPIError as api_error:
        logging.warning(
            "Не удалось продлить ключ", extra={"username": username, "error": api_error.code, "status": api_error.status}
        )
        if api_error.code == "user_not_found":
            await message.answer(
                "⚠️ Активный ключ не найден. Нажми «Получить VPN», чтобы создать новый.",
                reply_markup=main_kb,
            )
        elif api_error.code == "invalid_days":
            await message.answer("⚠️ Некорректный срок продления.", reply_markup=main_kb)
        else:
            status_info = f" (код {api_error.status})" if api_error.status else ""
            await message.answer(
                "⚠️ Не получилось продлить ключ." + status_info,
                reply_markup=main_kb,
            )
        return
    except Exception:
        logging.exception("Сбой при продлении VPN-ключа", extra={"username": username})
        await message.answer(
            "⚠️ Произошла ошибка при продлении. Попробуй снова позже.",
            reply_markup=main_kb,
        )
        return

    await message.answer(
        "✅ Ключ успешно продлён!\n"
        f"Новый срок действия до: {info.expires_at}",
        reply_markup=main_kb,
    )


async def send_key_status(message: Message, username: str) -> None:
    try:
        payload = await vpn_api.get_my_key(username=username, chat_id=message.chat.id)
    except VPNAPIError as api_error:
        logging.warning(
            "Не удалось получить статус ключа",
            extra={"username": username, "error": api_error.code, "status": api_error.status},
        )
        await message.answer("⚠️ Не удалось получить информацию о ключе. Попробуй позже.", reply_markup=main_kb)
        return
    except Exception:
        logging.exception("Сбой при запросе статуса ключа", extra={"username": username})
        await message.answer("⚠️ Произошла ошибка. Попробуй снова позже.", reply_markup=main_kb)
        return

    if not payload.get("ok"):
        await message.answer("ℹ️ Активный ключ не найден. Нажми «Получить VPN», чтобы создать новый.", reply_markup=main_kb)
        return

    link = payload.get("link")
    expires = payload.get("expires_at")
    uuid_value = payload.get("uuid")
    text = (
        "🔐 Твой текущий VPN-ключ\n"
        f"UUID: <code>{uuid_value}</code>\n"
        f"Ссылка: {link}\n"
        f"Действует до: {expires}"
    )
    await message.answer(text, reply_markup=main_kb)


@dp.message(CommandStart())
async def start_cmd(message: Message):
    username = save_user(message)
    text = (
        f"👋 Привет, {message.from_user.first_name or username}!\n\n"
        f"Я — AI-ассистент <b>VPN_GPT</b>.\n"
        "Помогу подобрать VPN и мгновенно выдать демо-ключ.\n"
        "⚙️ Пока тестовый период — бесплатно.\n\n"
        "Выбери действие ниже или просто напиши, что нужно 👇"
    )
    await message.answer(text, reply_markup=main_kb)
    await issue_and_send_key(message, username)


@dp.message(Command("buy"))
async def buy_cmd(message: Message):
    username = save_user(message)
    await issue_and_send_key(message, username)


@dp.message(Command("renew"))
async def renew_cmd(message: Message):
    username = save_user(message)
    await renew_vpn_key(message, username)


@dp.message(Command("mykey"))
async def my_key_cmd(message: Message):
    username = save_user(message)
    await send_key_status(message, username)


@dp.message(Command("admin"))
async def admin_cmd(message: Message):
    username = save_user(message)
    if not ADMIN_ID or str(message.from_user.id) != str(ADMIN_ID):
        await message.answer("⛔️ Эта команда доступна только администратору.", reply_markup=main_kb)
        return

    try:
        users_payload = await vpn_api.list_users()
    except VPNAPIError as api_error:
        logging.warning("Не удалось получить список пользователей", extra={"error": api_error.code})
        await message.answer("⚠️ Не получилось получить статистику. Попробуй позже.", reply_markup=main_kb)
        return
    except Exception:
        logging.exception("Сбой при запросе списка пользователей", extra={"username": username})
        await message.answer("⚠️ Произошла ошибка. Попробуй ещё раз позднее.", reply_markup=main_kb)
        return

    users = users_payload.get("users", [])
    total = len(users)
    active_links = sum(1 for item in users if item.get("active"))
    text = (
        "🛠 <b>Админ-панель</b>\n"
        f"Всего записей: {total}\n"
        f"Активных ключей: {active_links}"
    )
    await message.answer(text, reply_markup=main_kb)

@dp.message()
async def handle_message(message: Message):
    username = save_user(message)
    user_text = (message.text or "").strip()

    normalized = user_text.lower()
    if normalized in {"/buy", "buy", "получить vpn", "получить доступ"} or user_text == "💡 Получить VPN":
        await issue_and_send_key(message, username)
        return

    if normalized in {"/renew", "renew", "продлить", "продлить vpn"} or user_text == "♻️ Продлить VPN":
        await renew_vpn_key(message, username)
        return

    if normalized in {"/mykey", "мой ключ", "ключ", "посмотреть ключ"}:
        await send_key_status(message, username)
        return

    if normalized == "/admin":
        await admin_cmd(message)
        return

    # Визуальный отклик — бот «думает»
    await message.answer("✉️ Обрабатываю запрос...", reply_markup=main_kb)

    try:
        completion = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Пользователь Telegram @"
                        f"{username}. Отвечай кратко, дружелюбно и по сути."
                        " Если текст — 'Получить VPN' или 'Продлить VPN', инициируй"
                        " соответствующий сценарий через OpenAPI."
                    ),
                },
                {"role": "user", "content": user_text},
            ]
        )
        gpt_reply = completion.choices[0].message.content.strip()
        await message.answer(gpt_reply, reply_markup=main_kb)
        logging.info(f"GPT ответил @{username}: {gpt_reply}")

    except Exception as e:
        logging.error(f"Ошибка GPT при ответе @{username}: {e}")
        await message.answer("⚠️ Произошла ошибка при обращении к AI. Попробуй позже.", reply_markup=main_kb)

# === Запуск ===
async def main():
    ensure_tables()
    logging.info("Бот VPN_GPT запущен и готов принимать сообщения.")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())


# === Middleware: автообновление chat_id ===
class UpdateChatIDMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[Message, Dict[str, Any]], Awaitable[Any]],
        event: Message,
        data: Dict[str, Any],
    ) -> Any:
        try:
            if event.from_user and event.chat:
                with sqlite3.connect(DB_PATH) as conn:
                    conn.execute(
                        "INSERT OR REPLACE INTO tg_users (username, chat_id) VALUES (?, ?)",
                        (event.from_user.username, event.chat.id),
                    )
        except Exception as exc:
            logging.warning("Не удалось обновить chat_id", exc_info=True, extra={"error": str(exc)})
        return await handler(event, data)


dp.message.middleware(UpdateChatIDMiddleware())
print("✅ Middleware UpdateChatIDMiddleware активирован")
