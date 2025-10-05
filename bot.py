import asyncio
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from openai import AsyncOpenAI
from config import BOT_TOKEN, GPT_API_KEY, GPT_ASSISTANT_ID
from utils.vpn import add_vpn_user
from utils.qrgen import make_qr
from utils.db import init_db, save_message, get_last_messages

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
client = AsyncOpenAI(api_key=GPT_API_KEY)

# Старт
@dp.message(Command("start"))
async def start(msg: types.Message):
    await msg.answer(
        f"👋 Привет, {msg.from_user.first_name or 'друг'}!\n"
        "Я — VPN GPT, твой личный помощник по VPN.\n\n"
        "Отправь /buy чтобы получить подключение\n"
        "или просто задай вопрос 👇"
    )

# Покупка
@dp.message(Command("buy"))
async def buy(msg: types.Message):
    await msg.answer("⏳ Создаю подключение...")
    link = add_vpn_user()
    qr = make_qr(link)
    await msg.answer("✅ Ваш VPN готов!\nВот ссылка для подключения:")
    await msg.answer(link)
    await msg.answer_photo(qr, caption="📱 Отсканируйте QR-код для быстрого подключения")

# Общение с GPT
@dp.message()
async def chat_with_assistant(msg: types.Message):
    user_input = msg.text.strip()
    user_id = msg.from_user.id
    username = msg.from_user.username or ""
    first_name = msg.from_user.first_name or "друг"
    last_name = msg.from_user.last_name or ""
    full_name = f"{first_name} {last_name}".strip()

    # Получаем последние сообщения
    history = get_last_messages(user_id)
    context = "\n".join([f"Пользователь: {h[0]}\nGPT: {h[1]}" for h in history])

    system_prompt = (
        f"Ты — AI-консультант VPN GPT. "
        f"Ты общаешься с пользователем по имени {full_name}. "
        f"Вот краткий контекст предыдущих диалогов:\n{context}\n\n"
        f"Адаптируй стиль под собеседника и помоги выбрать VPN BusinessVPN. "
        f"Если готов — предложи команду /buy."
    )

    try:
        thread = await client.beta.threads.create_and_run(
            assistant_id=GPT_ASSISTANT_ID,
            thread={
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_input},
                ]
            }
        )

        messages = await client.beta.threads.messages.list(thread_id=thread.id)
        if messages.data:
            reply = messages.data[0].content[0].text.value
            await msg.answer(reply)
            save_message(user_id, username, full_name, user_input, reply)
        else:
            await msg.answer("⚠️ GPT не прислал ответ. Попробуй позже.")

    except Exception as e:
        print("Error communicating with GPT Assistant:", e)
        await msg.answer("⚠️ Произошла ошибка при обращении к GPT. Попробуй позже.")

async def main():
    init_db()
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())

