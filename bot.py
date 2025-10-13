"""Telegram bridge bot that proxies all user messages to GPT."""
from __future__ import annotations

import asyncio
import logging
import os
from collections import deque
from typing import Deque, Dict, List

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart
from aiogram.types import Message, MenuButtonDefault
from dotenv import load_dotenv
from openai import OpenAI

load_dotenv("/root/VPN_GPT/.env")

BOT_TOKEN = os.getenv("BOT_TOKEN")
GPT_API_KEY = os.getenv("GPT_API_KEY")
GPT_MODEL = os.getenv("GPT_MODEL", "gpt-4o-mini")
SYSTEM_PROMPT = os.getenv(
    "GPT_SYSTEM_PROMPT",
    "Ты — VPN_GPT, эксперт по VPN. Отвечай дружелюбно и помогай пользователю.",
)
MAX_HISTORY_MESSAGES = int(os.getenv("GPT_HISTORY_MESSAGES", "6"))

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


@dp.message(CommandStart())
async def handle_start(message: Message) -> None:
    await handle_message(message)


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
