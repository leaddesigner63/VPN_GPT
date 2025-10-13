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

VPN_API_URL = os.getenv("VPN_API_URL", "https://vpn-gpt.store/api")
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


async def request_key_info(username: str, chat_id: int | None = None) -> dict:
    params = {"username": username}
    if chat_id is not None:
        params["chat_id"] = chat_id

    async with httpx.AsyncClient(timeout=10.0) as client:
        response = await client.get(
            f"{VPN_API_URL.rstrip('/')}/vpn/my_key",
            params=params,
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
        uuid_value = payload.get("uuid")
        expires = payload.get("expires_at")
        is_active = payload.get("active")
        if not link:
            raise ValueError("Пустая ссылка от API")

        if is_active is True:
            status_text = "активен"
        elif is_active is False:
            status_text = "неактивен"
        else:
            status_text = "неизвестен"
        info_lines = [
            "🎁 Твой бесплатный VPN-ключ готов!",
            "",
            "🔐 Информация о ключе:",
        ]
        if uuid_value:
            info_lines.append(f"UUID: {uuid_value}")
        if expires:
            info_lines.append(f"Действует до: {expires}")
        if is_active is not None:
            info_lines.append(f"Статус: {status_text}")
        info_lines.append("🔗 Ссылка:")
        info_lines.append(link)

        await msg.answer("\n".join(info_lines))
        qr = make_qr(link)
        await msg.answer_photo(
            BufferedInputFile(qr.getvalue(), filename="vpn_key.png"),
            caption="📱 Отсканируй QR-код для быстрого подключения",
        )
    except Exception:
        await msg.answer("⚠️ Не удалось получить ключ. Попробуй чуть позже.")


@dp.message(Command("mykey"))
async def my_key(msg: Message):
    username = msg.from_user.username or f"id_{msg.from_user.id}"
    await msg.answer("🔎 Проверяю информацию о твоём ключе…")

    try:
        payload = await request_key_info(username, chat_id=msg.chat.id)
    except Exception:
        await msg.answer("⚠️ Не удалось получить информацию о ключе. Попробуй чуть позже.")
        return

    if not payload.get("ok"):
        await msg.answer("ℹ️ Активный ключ не найден. Нажми /buy, чтобы получить новый.")
        return

    link = payload.get("link")
    uuid_value = payload.get("uuid")
    expires = payload.get("expires_at")
    is_active = payload.get("active")
    if is_active is True:
        status_text = "активен"
    elif is_active is False:
        status_text = "неактивен"
    else:
        status_text = "неизвестен"

    info_lines = [
        "🔐 Информация о твоём VPN-ключе:",
    ]
    info_lines.append(f"Пользователь: {payload.get('username', username)}")
    if uuid_value:
        info_lines.append(f"UUID: {uuid_value}")
    if expires:
        info_lines.append(f"Действует до: {expires}")
    info_lines.append(f"Статус: {status_text}")
    if link:
        info_lines.append("🔗 Ссылка:")
        info_lines.append(link)

    await msg.answer("\n".join(info_lines))

    if link:
        qr = make_qr(link)
        await msg.answer_photo(
            BufferedInputFile(qr.getvalue(), filename="vpn_key.png"),
            caption="📱 Отсканируй QR-код для быстрого подключения",
        )


async def main():
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())

