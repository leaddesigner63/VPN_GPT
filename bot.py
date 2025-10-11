import os
import logging
import asyncio
import sqlite3
from datetime import datetime
from typing import Any, Awaitable, Callable, Dict

import httpx
from dotenv import load_dotenv
from aiogram import BaseMiddleware, Bot, Dispatcher
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart
from aiogram.types import (
    BufferedInputFile,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)
from aiogram.client.default import DefaultBotProperties
from openai import OpenAI

from utils.qrgen import make_qr

# === Инициализация ===
load_dotenv("/root/VPN_GPT/.env")

BOT_TOKEN = os.getenv("BOT_TOKEN")
GPT_API_KEY = os.getenv("GPT_API_KEY")
ADMIN_ID = os.getenv("ADMIN_ID")
VPN_API_URL = os.getenv("VPN_API_URL", "http://127.0.0.1:8000")
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "")

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
client = OpenAI(api_key=GPT_API_KEY)

DB_PATH = "/root/VPN_GPT/dialogs.db"

# === Логирование ===
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler("/root/VPN_GPT/bot.log"), logging.StreamHandler()]
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
def ensure_tables():
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS tg_users (
                username TEXT PRIMARY KEY,
                chat_id INTEGER,
                first_name TEXT,
                last_name TEXT,
                created_at TEXT
            )
        """)
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
async def _request_vpn_key(username: str, days: int = 30) -> dict[str, Any]:
    params = {"x-admin-token": ADMIN_TOKEN} if ADMIN_TOKEN else None
    async with httpx.AsyncClient(timeout=10.0) as client:
        response = await client.post(
            f"{VPN_API_URL.rstrip('/')}/vpn/issue_key",
            json={"username": username, "days": days},
            params=params,
        )
    response.raise_for_status()
    return response.json()


async def issue_and_send_key(message: Message, username: str) -> None:
    await message.answer("⏳ Создаю тебе VPN-ключ…", reply_markup=main_kb)
    try:
        payload = await _request_vpn_key(username)
        link = payload.get("link")
        if not link:
            raise ValueError("API не вернул ссылку для подключения")

        await message.answer(
            "🎁 Твой бесплатный VPN-ключ готов!\n\n"
            f"🔗 Ссылка:\n{link}",
            reply_markup=main_kb,
        )

        qr_stream = make_qr(link)
        await message.answer_photo(
            BufferedInputFile(qr_stream.getvalue(), filename="vpn_key.png"),
            caption="📱 Отсканируй QR-код для быстрого подключения",
        )
    except httpx.HTTPStatusError as http_err:
        status = http_err.response.status_code
        logging.error(
            "API вернул ошибку при выдаче ключа", exc_info=True, extra={"username": username, "status": status}
        )
        if status == 409:
            await message.answer(
                "ℹ️ У тебя уже есть активный VPN-ключ. Проверь предыдущие сообщения или продли текущий.",
                reply_markup=main_kb,
            )
        else:
            await message.answer(
                "⚠️ Не получилось создать ключ. Попробуй ещё раз позже."
                f"\nКод ошибки: {status}",
                reply_markup=main_kb,
            )
        logging.debug("API response body: %s", http_err.response.text)
    except Exception as err:
        logging.exception("Сбой при выдаче VPN-ключа", extra={"username": username})
        await message.answer(
            "⚠️ Не получилось создать ключ. Попробуй ещё раз чуть позже.",
            reply_markup=main_kb,
        )


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

@dp.message()
async def handle_message(message: Message):
    username = save_user(message)
    user_text = (message.text or "").strip()

    normalized = user_text.lower()
    if normalized in {"/buy", "buy", "получить vpn", "получить доступ"} or user_text == "💡 Получить VPN":
        await issue_and_send_key(message, username)
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
