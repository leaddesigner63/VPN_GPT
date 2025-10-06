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

