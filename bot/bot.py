import asyncio
import os
from typing import Any
from urllib.parse import urlparse

import httpx
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    MenuButtonDefault,
    Message,
)

from config import BOT_TOKEN
from utils.qrgen import make_qr

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

VPN_API_URL = os.getenv("VPN_API_URL", "https://vpn-gpt.store/api")
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "")
RENEW_DAYS = int(os.getenv("VPN_RENEW_DAYS", "30"))
_ALLOWED_BUTTON_SCHEMES = {"http", "https", "tg"}


def build_main_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔑 Получить новый ключ", callback_data="issue_key")],
            [InlineKeyboardButton(text="♻️ Продлить доступ", callback_data="renew_key")],
            [InlineKeyboardButton(text="📄 Мой ключ", callback_data="get_key")],
        ]
    )


def _is_supported_button_link(link: str) -> bool:
    """Return True when link is safe to use as a Telegram button URL."""

    if not link:
        return False

    try:
        parsed = urlparse(link)
    except ValueError:
        return False

    if parsed.scheme not in _ALLOWED_BUTTON_SCHEMES:
        return False

    if parsed.scheme in {"http", "https"}:
        return bool(parsed.netloc)

    # Telegram-specific deeplinks (tg://) rely on the path component.
    if parsed.scheme == "tg":
        return bool(parsed.path)

    return False


def build_result_markup(link: str | None = None) -> InlineKeyboardMarkup:
    buttons: list[list[InlineKeyboardButton]] = []
    if link:
        normalized_link = link.strip()
        if normalized_link and _is_supported_button_link(normalized_link):
            buttons.append([InlineKeyboardButton(text="🔗 Открыть ссылку", url=normalized_link)])
    buttons.append([InlineKeyboardButton(text="⬅️ Главное меню", callback_data="show_menu")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def format_key_info(payload: dict[str, Any], username: str, title: str) -> tuple[str, str | None]:
    lines: list[str] = [title]

    payload_username = payload.get("username")
    if payload_username:
        lines.append(f"Пользователь: {payload_username}")
    else:
        lines.append(f"Пользователь: {username}")

    uuid_value = payload.get("uuid")
    if uuid_value:
        lines.append(f"UUID: {uuid_value}")

    expires = payload.get("expires_at")
    if expires:
        lines.append(f"Действует до: {expires}")

    active = payload.get("active")
    if active is not None:
        status_text = "активен" if active else "неактивен"
        lines.append(f"Статус: {status_text}")

    link = payload.get("link")
    if link:
        lines.append("")
        lines.append("🔗 Ссылка для подключения:")
        lines.append(link)

    return "\n".join(lines), link


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


async def renew_key(username: str, days: int = RENEW_DAYS) -> dict:
    params = {"x-admin-token": ADMIN_TOKEN} if ADMIN_TOKEN else None
    async with httpx.AsyncClient(timeout=10.0) as client:
        response = await client.post(
            f"{VPN_API_URL.rstrip('/')}/vpn/renew_key",
            params=params,
            json={"username": username, "days": days},
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
        "👋 Привет! Я бот VPN_GPT. Сейчас тестовый период — ключи выдаются бесплатно.\n"
        "\nВыбери действие в меню ниже, и я всё сделаю за тебя.",
        reply_markup=build_main_menu(),
    )


@dp.message(Command("buy"))
async def buy(msg: Message):
    username = msg.from_user.username or f"id_{msg.from_user.id}"
    await handle_issue_key(msg, username)


@dp.message(Command("mykey"))
async def my_key(msg: Message):
    username = msg.from_user.username or f"id_{msg.from_user.id}"
    await handle_get_key(msg, username, msg.chat.id)


@dp.message(Command("renew"))
async def renew(msg: Message):
    username = msg.from_user.username or f"id_{msg.from_user.id}"
    await handle_renew_key(msg, username, msg.chat.id)


async def handle_issue_key(message: Message, username: str) -> None:
    progress = await message.answer("⏳ Создаю для тебя VPN-ключ…")
    try:
        payload = await request_key(username)
    except Exception:
        await progress.edit_text(
            "⚠️ Не удалось получить ключ. Попробуй ещё раз чуть позже.",
            reply_markup=build_result_markup(),
        )
        return

    if not payload.get("ok"):
        await progress.edit_text(
            "⚠️ Не удалось получить ключ. Попробуй ещё раз чуть позже.",
            reply_markup=build_result_markup(),
        )
        return

    text, link = format_key_info(payload, username, "🎁 Твой VPN-ключ готов!")
    await progress.edit_text(text, reply_markup=build_result_markup(link))

    if link:
        qr = make_qr(link)
        await message.answer_photo(
            BufferedInputFile(qr.getvalue(), filename="vpn_key.png"),
            caption="📱 Отсканируй QR-код для быстрого подключения",
        )


async def handle_get_key(message: Message, username: str, chat_id: int) -> None:
    progress = await message.answer("🔎 Проверяю информацию о твоём ключе…")

    try:
        payload = await request_key_info(username, chat_id=chat_id)
    except Exception:
        await progress.edit_text(
            "⚠️ Не удалось получить информацию о ключе. Попробуй позже.",
            reply_markup=build_result_markup(),
        )
        return

    if not payload.get("ok"):
        await progress.edit_text(
            "ℹ️ Активный ключ не найден. Нажми кнопку \"Получить новый ключ\" в меню.",
            reply_markup=build_result_markup(),
        )
        return

    text, link = format_key_info(payload, username, "🔐 Информация о твоём VPN-ключе:")
    await progress.edit_text(text, reply_markup=build_result_markup(link))

    if link:
        qr = make_qr(link)
        await message.answer_photo(
            BufferedInputFile(qr.getvalue(), filename="vpn_key.png"),
            caption="📱 Отсканируй QR-код для быстрого подключения",
        )


async def handle_renew_key(message: Message, username: str, chat_id: int) -> None:
    progress = await message.answer("♻️ Продлеваю срок действия твоего ключа…")

    try:
        renew_payload = await renew_key(username)
    except Exception:
        await progress.edit_text(
            "⚠️ Не удалось продлить доступ. Попробуй ещё раз позже.",
            reply_markup=build_result_markup(),
        )
        return

    if not renew_payload.get("ok"):
        detail = renew_payload.get("detail") or "Не удалось продлить доступ."
        await progress.edit_text(f"⚠️ {detail}", reply_markup=build_result_markup())
        return

    try:
        info_payload = await request_key_info(username, chat_id=chat_id)
    except Exception:
        info_payload = None

    if info_payload and info_payload.get("ok"):
        text, link = format_key_info(info_payload, username, "♻️ Доступ успешно продлён!")
    else:
        expires = renew_payload.get("expires_at")
        lines = ["♻️ Доступ успешно продлён!"]
        if expires:
            lines.append(f"Новая дата окончания: {expires}")
        link = None
        text = "\n".join(lines)

    await progress.edit_text(text, reply_markup=build_result_markup(link))

    if link:
        qr = make_qr(link)
        await message.answer_photo(
            BufferedInputFile(qr.getvalue(), filename="vpn_key.png"),
            caption="📱 Отсканируй QR-код для быстрого подключения",
        )


@dp.callback_query(F.data == "issue_key")
async def issue_key_callback(callback: CallbackQuery):
    await callback.answer()
    if not callback.message:
        return
    username = callback.from_user.username or f"id_{callback.from_user.id}"
    await handle_issue_key(callback.message, username)


@dp.callback_query(F.data == "renew_key")
async def renew_key_callback(callback: CallbackQuery):
    await callback.answer()
    if not callback.message:
        return
    username = callback.from_user.username or f"id_{callback.from_user.id}"
    await handle_renew_key(callback.message, username, callback.message.chat.id)


@dp.callback_query(F.data == "get_key")
async def get_key_callback(callback: CallbackQuery):
    await callback.answer()
    if not callback.message:
        return
    username = callback.from_user.username or f"id_{callback.from_user.id}"
    await handle_get_key(callback.message, username, callback.message.chat.id)


@dp.callback_query(F.data == "show_menu")
async def show_menu(callback: CallbackQuery):
    await callback.answer()
    if not callback.message:
        return
    await callback.message.answer(
        "Выбери нужное действие:",
        reply_markup=build_main_menu(),
    )


async def main():
    try:
        await bot.delete_my_commands()
        await bot.set_chat_menu_button(MenuButtonDefault())
    except Exception:
        # Для простого бота ограничимся сообщением в stdout.
        print("⚠️ Не удалось очистить меню команд бота", flush=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())

