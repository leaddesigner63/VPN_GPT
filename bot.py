"""Telegram bridge bot that proxies all user messages to GPT."""
from __future__ import annotations

import asyncio
import logging
import os
from collections import deque
from typing import Any, Deque, Dict, List
from urllib.parse import urlparse

import httpx
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    MenuButtonDefault,
    Message,
)
from dotenv import load_dotenv
from openai import OpenAI
from utils.qrgen import make_qr

load_dotenv("/root/VPN_GPT/.env")

BOT_TOKEN = os.getenv("BOT_TOKEN")
GPT_API_KEY = os.getenv("GPT_API_KEY")
GPT_MODEL = os.getenv("GPT_MODEL", "gpt-4o-mini")
SYSTEM_PROMPT = os.getenv(
    "GPT_SYSTEM_PROMPT",
    "Ты — VPN_GPT, эксперт по VPN. Отвечай дружелюбно и помогай пользователю.",
)
MAX_HISTORY_MESSAGES = int(os.getenv("GPT_HISTORY_MESSAGES", "6"))
VPN_API_URL = os.getenv("VPN_API_URL", "https://vpn-gpt.store/api")
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "")
RENEW_DAYS = int(os.getenv("VPN_RENEW_DAYS", "30"))
_ALLOWED_BUTTON_SCHEMES = {"http", "https", "tg"}

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is not configured")
if not GPT_API_KEY:
    raise RuntimeError("GPT_API_KEY is not configured")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("vpn_gpt.bot")

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
client = OpenAI(api_key=GPT_API_KEY)

ConversationHistory = Deque[dict[str, str]]
_histories: Dict[int, ConversationHistory] = {}


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

    # Telegram-specific deeplinks (tg://) may rely on path, netloc or query params.
    if parsed.scheme == "tg":
        return bool(parsed.path or parsed.netloc or parsed.query)

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
    async with httpx.AsyncClient(timeout=10.0) as client_http:
        response = await client_http.post(
            f"{VPN_API_URL.rstrip('/')}/vpn/issue_key",
            params=params,
            json={"username": username},
        )
    response.raise_for_status()
    return response.json()


async def renew_key(username: str, days: int = RENEW_DAYS) -> dict:
    params = {"x-admin-token": ADMIN_TOKEN} if ADMIN_TOKEN else None
    async with httpx.AsyncClient(timeout=10.0) as client_http:
        response = await client_http.post(
            f"{VPN_API_URL.rstrip('/')}/vpn/renew_key",
            params=params,
            json={"username": username, "days": days},
        )
    response.raise_for_status()
    return response.json()


async def request_key_info(username: str, chat_id: int | None = None) -> dict:
    params: dict[str, Any] = {"username": username}
    if chat_id is not None:
        params["chat_id"] = chat_id

    async with httpx.AsyncClient(timeout=10.0) as client_http:
        response = await client_http.get(
            f"{VPN_API_URL.rstrip('/')}/vpn/my_key",
            params=params,
        )
    response.raise_for_status()
    return response.json()


def _get_history(chat_id: int) -> ConversationHistory:
    history = _histories.get(chat_id)
    if history is None:
        maxlen = MAX_HISTORY_MESSAGES * 2 if MAX_HISTORY_MESSAGES > 0 else None
        history = deque(maxlen=maxlen)
        _histories[chat_id] = history
    return history


def _build_messages(chat_id: int, user_text: str) -> List[dict[str, str]]:
    history = _get_history(chat_id)
    messages: List[dict[str, str]] = []
    if SYSTEM_PROMPT:
        messages.append({"role": "system", "content": SYSTEM_PROMPT})
    messages.extend(history)
    messages.append({"role": "user", "content": user_text})
    return messages


def _remember_exchange(chat_id: int, user_text: str, reply: str) -> None:
    history = _get_history(chat_id)
    history.append({"role": "user", "content": user_text})
    history.append({"role": "assistant", "content": reply})


async def _ask_gpt(chat_id: int, username: str, user_text: str) -> str:
    messages = _build_messages(chat_id, user_text)
    logger.info("Forwarding message from @%s to GPT", username)
    completion = client.chat.completions.create(model=GPT_MODEL, messages=messages)
    reply = completion.choices[0].message.content or ""
    _remember_exchange(chat_id, user_text, reply)
    logger.info("GPT replied to @%s: %s", username, reply)
    return reply


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


@dp.message(CommandStart())
async def handle_start(message: Message) -> None:
    await message.answer(
        "👋 Привет! Я бот VPN_GPT. Помогу тебе получить и управлять VPN-доступом.\n"
        "\nВыбери действие в меню ниже или просто напиши вопрос.",
        reply_markup=build_main_menu(),
    )


@dp.message(Command("menu"))
async def handle_menu_command(message: Message) -> None:
    await message.answer("Выбери нужное действие:", reply_markup=build_main_menu())


@dp.message(Command("buy"))
async def handle_buy_command(message: Message) -> None:
    username = message.from_user.username or f"id_{message.from_user.id}"
    await handle_issue_key(message, username)


@dp.message(Command("renew"))
async def handle_renew_command(message: Message) -> None:
    username = message.from_user.username or f"id_{message.from_user.id}"
    await handle_renew_key(message, username, message.chat.id)


@dp.message(Command("mykey"))
async def handle_mykey_command(message: Message) -> None:
    username = message.from_user.username or f"id_{message.from_user.id}"
    await handle_get_key(message, username, message.chat.id)


@dp.message()
async def handle_message(message: Message) -> None:
    if not message.text:
        await message.answer("Пожалуйста, отправь текстовое сообщение.")
        return

    username = message.from_user.username or f"id_{message.from_user.id}"
    user_text = message.text.strip()

    try:
        reply = await _ask_gpt(message.chat.id, username, user_text)
    except Exception:
        logger.exception("Failed to obtain GPT response for @%s", username)
        await message.answer("⚠️ Не удалось получить ответ от GPT. Попробуй позже.")
        return

    await message.answer(reply)


@dp.callback_query(F.data == "issue_key")
async def issue_key_callback(callback: CallbackQuery) -> None:
    await callback.answer()
    if not callback.message:
        return
    username = callback.from_user.username or f"id_{callback.from_user.id}"
    await handle_issue_key(callback.message, username)


@dp.callback_query(F.data == "renew_key")
async def renew_key_callback(callback: CallbackQuery) -> None:
    await callback.answer()
    if not callback.message:
        return
    username = callback.from_user.username or f"id_{callback.from_user.id}"
    await handle_renew_key(callback.message, username, callback.message.chat.id)


@dp.callback_query(F.data == "get_key")
async def get_key_callback(callback: CallbackQuery) -> None:
    await callback.answer()
    if not callback.message:
        return
    username = callback.from_user.username or f"id_{callback.from_user.id}"
    await handle_get_key(callback.message, username, callback.message.chat.id)


@dp.callback_query(F.data == "show_menu")
async def show_menu_callback(callback: CallbackQuery) -> None:
    await callback.answer()
    if not callback.message:
        return
    await callback.message.answer("Выбери нужное действие:", reply_markup=build_main_menu())


async def clear_bot_menu() -> None:
    try:
        await bot.delete_my_commands()
        await bot.set_chat_menu_button(MenuButtonDefault())
    except Exception:
        logger.exception("Unable to reset the bot menu")


async def main() -> None:
    await clear_bot_menu()
    logger.info("VPN_GPT relay bot started")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
