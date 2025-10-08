import asyncio
import os
from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, F
from aiogram.types import Message
from aiogram.filters import CommandStart
from openai import OpenAI
from api.utils import db

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
GPT_API_KEY = os.getenv("GPT_API_KEY")
GPT_ASSISTANT_ID = os.getenv("GPT_ASSISTANT_ID")

db.init_db()

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

client = OpenAI(api_key=GPT_API_KEY)

async def ensure_thread(user_id: str) -> str:
    t = db.get_thread(user_id)
    if t:
        return t
    thread = client.beta.threads.create()
    db.upsert_thread(user_id, thread.id)
    return thread.id

async def run_assistant(thread_id: str, text: str) -> str:
    # 1) add message
    client.beta.threads.messages.create(
        thread_id=thread_id,
        role="user",
        content=text
    )
    # 2) run
    run = client.beta.threads.runs.create(
        thread_id=thread_id,
        assistant_id=GPT_ASSISTANT_ID,
    )
    # 3) poll
    for _ in range(60):  # до 60с
        r = client.beta.threads.runs.retrieve(thread_id=thread_id, run_id=run.id)
        if r.status in ("completed", "failed", "cancelled", "expired"):
            break
        await asyncio.sleep(1)
    if r.status != "completed":
        return f"⚠️ Ошибка: статус {r.status}"
    # 4) read last assistant message(s)
    msgs = client.beta.threads.messages.list(thread_id=thread_id, order="desc", limit=5)
    for m in msgs.data:
        if m.role == "assistant":
            # content может быть списком; возьмём первый текст
            parts = m.content
            for p in parts:
                if p.type == "text":
                    return p.text.value[:4096]
    return "⚠️ Пустой ответ"

@dp.message(CommandStart())
async def start(m: Message):
    await m.answer(
        "Привет! Я AI-ассистент по VPN. Задайте вопрос или введите /buy для покупки.\n"
        "Если вы уже оплачивали — просто напишите, и я проверю статус."
    )

@dp.message(F.text)
async def on_text(m: Message):
    user_id = str(m.from_user.id)
    thread_id = await ensure_thread(user_id)
    reply = await run_assistant(thread_id, m.text)
    await m.answer(reply)

if __name__ == "__main__":
    import asyncio
    asyncio.run(dp.start_polling(bot))

# --- CHAT ID CAPTURE (safe middleware) ---
import sqlite3
from aiogram import types
try:
    from aiogram.dispatcher.middlewares import BaseMiddleware
except Exception:
    # aiogram v3 fallback (если вдруг используется v3)
    from aiogram.dispatcher.middlewares.base import BaseMiddleware  # type: ignore

DB_PATH = "/root/VPN_GPT/dialogs.db"

def _ensure_tables():
    with sqlite3.connect(DB_PATH) as conn:
        c = conn.cursor()
        c.execute("""CREATE TABLE IF NOT EXISTS tg_users (
            username   TEXT PRIMARY KEY,
            chat_id    INTEGER NOT NULL,
            first_seen TEXT DEFAULT (datetime('now')),
            last_seen  TEXT DEFAULT (datetime('now'))
        );""")
        conn.commit()

def save_chat_id(username: str | None, chat_id: int | None) -> None:
    if not username or not chat_id:
        return
    _ensure_tables()
    username = username.lstrip("@").lower()
    with sqlite3.connect(DB_PATH) as conn:
        c = conn.cursor()
        c.execute("""
            INSERT INTO tg_users (username, chat_id, first_seen, last_seen)
            VALUES (?, ?, datetime('now'), datetime('now'))
            ON CONFLICT(username) DO UPDATE SET
                chat_id=excluded.chat_id,
                last_seen=datetime('now')
        """, (username, int(chat_id)))
        conn.commit()

class SaveChatIdMiddleware(BaseMiddleware):
    async def on_pre_process_message(self, message: types.Message, data: dict):
        try:
            save_chat_id(getattr(message.from_user, "username", None), getattr(message.chat, "id", None))
        except Exception:
            pass

# dp должен быть объявлен выше в файле (Dispatcher)
try:
    dp.middleware.setup(SaveChatIdMiddleware())
except Exception:
    # Если порядок другой и dp ещё не создан — проигнорируем (но лучше поместить этот блок сразу после объявления dp)
    pass

@dp.message_handler(commands=["start"])
async def cmd_start(message: types.Message):
    save_chat_id(getattr(message.from_user, "username", None), getattr(message.chat, "id", None))
    await message.answer("Готово! Я запомнил твой chat_id ✅")
# --- /CHAT ID CAPTURE ---
# --- CHAT ID CAPTURE (safe middleware) ---
import sqlite3
from aiogram import types
try:
    from aiogram.dispatcher.middlewares import BaseMiddleware  # aiogram v2
except Exception:
    from aiogram.dispatcher.middlewares.base import BaseMiddleware  # aiogram v3 fallback

DB_PATH = "/root/VPN_GPT/dialogs.db"

def _ensure_tables():
    with sqlite3.connect(DB_PATH) as conn:
        c = conn.cursor()
        c.execute("""CREATE TABLE IF NOT EXISTS tg_users (
            username   TEXT PRIMARY KEY,
            chat_id    INTEGER NOT NULL,
            first_seen TEXT DEFAULT (datetime('now')),
            last_seen  TEXT DEFAULT (datetime('now'))
        );""")
        conn.commit()

def save_chat_id(username: str | None, chat_id: int | None) -> None:
    if not username or not chat_id:
        return
    _ensure_tables()
    username = username.lstrip("@").lower()
    with sqlite3.connect(DB_PATH) as conn:
        c = conn.cursor()
        c.execute("""
            INSERT INTO tg_users (username, chat_id, first_seen, last_seen)
            VALUES (?, ?, datetime('now'), datetime('now'))
            ON CONFLICT(username) DO UPDATE SET
                chat_id=excluded.chat_id,
                last_seen=datetime('now')
        """, (username, int(chat_id)))
        conn.commit()

class SaveChatIdMiddleware(BaseMiddleware):
    async def on_pre_process_message(self, message: types.Message, data: dict):
        try:
            save_chat_id(getattr(message.from_user, "username", None), getattr(message.chat, "id", None))
        except Exception:
            pass

# dp должен быть объявлен выше (Dispatcher)
try:
    dp.middleware.setup(SaveChatIdMiddleware())
except Exception:
    pass

@dp.message_handler(commands=["start"])
async def cmd_start(message: types.Message):
    save_chat_id(getattr(message.from_user, "username", None), getattr(message.chat, "id", None))
    await message.answer("Готово! Я запомнил твой chat_id ✅")
# --- /CHAT ID CAPTURE ---
# --- CHAT ID CAPTURE (safe middleware) ---
import sqlite3
from aiogram import types
try:
    from aiogram.dispatcher.middlewares import BaseMiddleware  # aiogram v2
except Exception:
    from aiogram.dispatcher.middlewares.base import BaseMiddleware  # aiogram v3 fallback
DB_PATH = "/root/VPN_GPT/dialogs.db"
def _ensure_tables():
    with sqlite3.connect(DB_PATH) as conn:
        c = conn.cursor()
        c.execute("""CREATE TABLE IF NOT EXISTS tg_users (
            username   TEXT PRIMARY KEY,
            chat_id    INTEGER NOT NULL,
            first_seen TEXT DEFAULT (datetime('now')),
            last_seen  TEXT DEFAULT (datetime('now'))
        );""")
        conn.commit()
def save_chat_id(username: str | None, chat_id: int | None) -> None:
    if not username or not chat_id: return
    _ensure_tables()
    username = username.lstrip("@").lower()
    with sqlite3.connect(DB_PATH) as conn:
        c = conn.cursor()
        c.execute("""
            INSERT INTO tg_users (username, chat_id, first_seen, last_seen)
            VALUES (?, ?, datetime('now'), datetime('now'))
            ON CONFLICT(username) DO UPDATE SET
                chat_id=excluded.chat_id,
                last_seen=datetime('now')
        """, (username, int(chat_id)))
        conn.commit()
class SaveChatIdMiddleware(BaseMiddleware):
    async def on_pre_process_message(self, message: types.Message, data: dict):
        try:
            save_chat_id(getattr(message.from_user, "username", None), getattr(message.chat, "id", None))
        except Exception:
            pass
try:
    dp.middleware.setup(SaveChatIdMiddleware())
except Exception:
    pass
@dp.message_handler(commands=["start"])
async def cmd_start(message: types.Message):
    save_chat_id(getattr(message.from_user, "username", None), getattr(message.chat, "id", None))
    await message.answer("Готово! Я запомнил твой chat_id ✅")
# --- /CHAT ID CAPTURE ---
# --- CHAT ID CAPTURE (safe middleware) ---
import sqlite3
from aiogram import types
try:
    from aiogram.dispatcher.middlewares import BaseMiddleware  # aiogram v2
except Exception:
    from aiogram.dispatcher.middlewares.base import BaseMiddleware  # aiogram v3 fallback
DB_PATH = "/root/VPN_GPT/dialogs.db"
def _ensure_tables():
    with sqlite3.connect(DB_PATH) as conn:
        c = conn.cursor()
        c.execute("""CREATE TABLE IF NOT EXISTS tg_users (
            username   TEXT PRIMARY KEY,
            chat_id    INTEGER NOT NULL,
            first_seen TEXT DEFAULT (datetime('now')),
            last_seen  TEXT DEFAULT (datetime('now'))
        );""")
        conn.commit()
def save_chat_id(username: str | None, chat_id: int | None) -> None:
    if not username or not chat_id: return
    _ensure_tables()
    username = username.lstrip("@").lower()
    with sqlite3.connect(DB_PATH) as conn:
        c = conn.cursor()
        c.execute("""
            INSERT INTO tg_users (username, chat_id, first_seen, last_seen)
            VALUES (?, ?, datetime('now'), datetime('now'))
            ON CONFLICT(username) DO UPDATE SET
                chat_id=excluded.chat_id,
                last_seen=datetime('now')
        """, (username, int(chat_id)))
        conn.commit()
class SaveChatIdMiddleware(BaseMiddleware):
    async def on_pre_process_message(self, message: types.Message, data: dict):
        try:
            save_chat_id(getattr(message.from_user, "username", None), getattr(message.chat, "id", None))
        except Exception:
            pass
try:
    dp.middleware.setup(SaveChatIdMiddleware())
except Exception:
    pass
@dp.message_handler(commands=["start"])
async def cmd_start(message: types.Message):
    save_chat_id(getattr(message.from_user, "username", None), getattr(message.chat, "id", None))
    await message.answer("Готово! Я запомнил твой chat_id ✅")
# --- /CHAT ID CAPTURE ---
