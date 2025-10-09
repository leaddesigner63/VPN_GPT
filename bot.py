import os
import logging
import asyncio
import sqlite3
from datetime import datetime
from dotenv import load_dotenv
from aiogram import Bot, Dispatcher
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart
from aiogram.types import Message, ReplyKeyboardMarkup, KeyboardButton
from aiogram.client.default import DefaultBotProperties
from openai import OpenAI

# === Инициализация ===
load_dotenv("/root/VPN_GPT/.env")

BOT_TOKEN = os.getenv("BOT_TOKEN")
GPT_API_KEY = os.getenv("GPT_API_KEY")
ADMIN_ID = os.getenv("ADMIN_ID")

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
        [KeyboardButton(text="💳 Купить VPN")],
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
@dp.message(CommandStart())
async def start_cmd(message: Message):
    username = save_user(message)
    text = (
        f"👋 Привет, {message.from_user.first_name or username}!\n\n"
        f"Я — AI-ассистент <b>VPN_GPT</b>.\n"
        f"Помогу подобрать, купить или продлить VPN.\n\n"
        f"Выбери действие ниже 👇"
    )
    await message.answer(text, reply_markup=main_kb)

@dp.message()
async def handle_message(message: Message):
    username = save_user(message)
    user_text = message.text.strip()

    # Визуальный отклик — бот «думает»
    await message.answer("✉️ Обрабатываю запрос...", reply_markup=main_kb)

    try:
        completion = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": f"Пользователь Telegram @{username}. Отвечай кратко, дружелюбно и по сути. Если текст — 'Купить VPN' или 'Продлить VPN', инициируй соответствующий сценарий через OpenAPI."},
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
from aiogram import BaseMiddleware
from typing import Callable, Dict, Any, Awaitable
import sqlite3

class UpdateChatIDMiddleware(BaseMiddleware):
    async def __call__(self,
        handler: Callable[[types.Message, Dict[str, Any]], Awaitable[Any]],
        event: types.Message,
        data: Dict[str, Any]
    ) -> Any:
        try:
            if event.from_user and event.chat:
                conn = sqlite3.connect(DB)
                cur = conn.cursor()
                cur.execute(
                    "INSERT OR REPLACE INTO tg_users (username, chat_id) VALUES (?, ?)",
                    (event.from_user.username, event.chat.id)
                )
                conn.commit(); conn.close()
        except Exception as e:
            print("chat_id save error:", e)
        return await handler(event, data)

dp.message.middleware(UpdateChatIDMiddleware())
print("✅ Middleware UpdateChatIDMiddleware активирован")
