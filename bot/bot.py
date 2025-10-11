import asyncio
import os

import httpx
from aiogram import Bot, Dispatcher
from aiogram.filters import Command
from aiogram.types import BufferedInputFile, Message

from config import BOT_TOKEN
from utils.qrgen import make_qr

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

VPN_API_URL = os.getenv("VPN_API_URL", "http://127.0.0.1:8000")
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "")


async def request_key(username: str) -> dict:
    params = {"x-admin-token": ADMIN_TOKEN} if ADMIN_TOKEN else None
    async with httpx.AsyncClient(timeout=10.0) as client:
        response = await client.post(
            f"{VPN_API_URL.rstrip('/')}/vpn/issue_key",
            params=params,
            json={"username": username},
        )
    response.raise_for_status()
    return response.json()


@dp.message(Command("start"))
async def start(msg: Message):
    await msg.answer(
        "👋 Привет! Я бот VPN_GPT. Сейчас тестовый период — ключи выдаются бесплатно."
    )
    await buy(msg)


@dp.message(Command("buy"))
async def buy(msg: Message):
    username = msg.from_user.username or f"id_{msg.from_user.id}"
    await msg.answer("⏳ Создаю тебе VPN-ключ…")
    try:
        payload = await request_key(username)
        link = payload.get("link")
        if not link:
            raise ValueError("Пустая ссылка от API")

        await msg.answer("🎁 Твой бесплатный VPN-ключ готов!\n\n🔗 Ссылка:\n" + link)
        qr = make_qr(link)
        await msg.answer_photo(
            BufferedInputFile(qr.getvalue(), filename="vpn_key.png"),
            caption="📱 Отсканируй QR-код для быстрого подключения",
        )
    except Exception:
        await msg.answer("⚠️ Не удалось получить ключ. Попробуй чуть позже.")


async def main():
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())

