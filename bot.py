import asyncio
import os
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from openai import AsyncOpenAI
from dotenv import load_dotenv
from utils.vpn import add_vpn_user
from utils.qrgen import make_qr

# --- Инициализация ---
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
GPT_ASSISTANT_ID = os.getenv("GPT_ASSISTANT_ID")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
client = AsyncOpenAI(api_key=OPENAI_API_KEY)

# --- Команда /start ---
@dp.message(Command("start"))
async def start(msg: types.Message):
    await msg.answer(
        "👋 Привет! Я — VPN GPT, твой персональный помощник по VPN.\n"
        "Расскажи, зачем тебе нужен VPN — я помогу подобрать оптимальный вариант."
    )

# --- Команда /buy ---
@dp.message(Command("buy"))
async def buy(msg: types.Message):
    await msg.answer("⏳ Создаю подключение...")
    link = add_vpn_user()
    qr = make_qr(link)
    await msg.answer("✅ Готово! Вот ссылка для подключения:")
    await msg.answer(link)
    await msg.answer_photo(qr, caption="📱 Отсканируй QR-код для быстрого подключения")

# --- Общение с кастомным GPT через Assistants API ---
@dp.message()
async def chat_with_assistant(msg: types.Message):
    user_input = msg.text.strip()

    try:
        thread = await client.beta.threads.create_and_run(
            assistant_id=GPT_ASSISTANT_ID,
            thread={"messages": [{"role": "user", "content": user_input}]}
        )

        # Получаем ответ из последнего сообщения
        messages = await client.beta.threads.messages.list(thread_id=thread.id)
        if messages.data:
            reply = messages.data[0].content[0].text.value
            await msg.answer(reply)
        else:
            await msg.answer("⚠️ GPT не прислал ответ. Попробуй позже.")

    except Exception as e:
        print("Error communicating with GPT Assistant:", e)
        await msg.answer("⚠️ Произошла ошибка при обращении к GPT. Попробуй позже.")

# --- Запуск ---
async def main():
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())

