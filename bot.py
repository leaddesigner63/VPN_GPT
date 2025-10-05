from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Dict, List, Optional, Tuple

from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.types import BufferedInputFile, ReplyKeyboardMarkup, ReplyKeyboardRemove
from openai import AsyncOpenAI

from config import ADMIN_ID, BOT_TOKEN, GPT_API_KEY, GPT_ASSISTANT_ID
from utils.db import (
    get_all_active_users,
    get_expired_keys,
    get_expiring_keys,
    get_last_messages,
    init_db,
    renew_vpn_key,
    save_message,
    save_vpn_key,
)
from utils.qrgen import make_qr
from utils.vpn import add_vpn_user


if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN environment variable is not configured")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
client = AsyncOpenAI(api_key=GPT_API_KEY) if GPT_API_KEY else None


DEFAULT_SUBSCRIPTION_DAYS = 30
EXPIRING_THRESHOLD_DAYS = 3
BROADCAST_TIMEOUT = timedelta(minutes=10)


pending_broadcast: Dict[int, datetime] = {}
notified_expiring: set[Tuple[int, datetime]] = set()


# === Проверка прав ===
def is_admin(user_id: int) -> bool:
    return ADMIN_ID is not None and user_id == ADMIN_ID


# === Команда /start ===
@dp.message(Command("start"))
async def start(msg: types.Message):
    if is_admin(msg.from_user.id):
        await msg.answer("🔧 Привет, админ! Отправь /admin чтобы открыть панель управления.")
    else:
        await msg.answer(
            f"👋 Привет, {msg.from_user.first_name or 'друг'}!\n"
            "Я — VPN GPT, твой личный помощник по VPN.\n\n"
            "Отправь /buy чтобы получить подключение\n"
            "или просто задай вопрос 👇"
        )


# === Админ панель ===
@dp.message(Command("admin"))
async def admin_panel(msg: types.Message):
    if not is_admin(msg.from_user.id):
        return await msg.answer("⛔ Доступ запрещён")

    keyboard = ReplyKeyboardMarkup(resize_keyboard=True)
    keyboard.add("/users", "/expired")
    keyboard.add("/broadcast")
    await msg.answer("⚙️ Панель администратора:", reply_markup=keyboard)


# === Список активных пользователей ===
@dp.message(Command("users"))
async def list_users(msg: types.Message):
    if not is_admin(msg.from_user.id):
        return await msg.answer("⛔ Доступ запрещён")

    users = get_all_active_users()
    if not users:
        return await msg.answer("👤 Активных пользователей нет.")

    text = "👥 Активные пользователи:\n\n"
    for u in users:
        uid, name, exp = u
        display_name = name or str(uid)
        display_date = exp[:10] if isinstance(exp, str) else exp
        text += f"• {display_name} — до {display_date} (ID: {uid})\n"
    await msg.answer(text)


# === Просроченные пользователи ===
@dp.message(Command("expired"))
async def expired_users(msg: types.Message):
    if not is_admin(msg.from_user.id):
        return await msg.answer("⛔ Доступ запрещён")

    expired = get_expired_keys()
    if not expired:
        return await msg.answer("✅ Нет просроченных подключений.")

    text = "🚫 Просроченные пользователи:\n\n"
    for user_id, full_name, _ in expired:
        text += f"• {full_name or user_id} (ID: {user_id})\n"
    await msg.answer(text)


# === Рассылка ===
@dp.message(Command("broadcast"))
async def broadcast(msg: types.Message):
    if not is_admin(msg.from_user.id):
        return await msg.answer("⛔ Доступ запрещён")

    pending_broadcast[msg.from_user.id] = datetime.now(UTC)
    await msg.answer(
        "📢 Введите текст рассылки."
        "\nОтправьте /cancel для отмены.",
        reply_markup=ReplyKeyboardRemove(),
    )


def _format_name(message: types.Message) -> str:
    user = message.from_user
    if not user:
        return "Неизвестный пользователь"
    if user.full_name:
        return user.full_name
    if user.username:
        return f"@{user.username}"
    return str(user.id)


def _default_user_keyboard() -> ReplyKeyboardRemove:
    return ReplyKeyboardRemove()


async def _send_qr(message: types.Message, link: str, expires_at: datetime) -> None:
    qr = make_qr(link)
    photo = BufferedInputFile(qr.getvalue(), filename="vpn_qr.png")
    expires_text = expires_at.strftime("%d.%m.%Y")
    caption = (
        "✅ Подключение готово!\n\n"
        f"📅 Доступ активен до {expires_text}.\n"
        "📱 Отсканируйте QR-код или используйте ссылку ниже."
    )
    await message.answer_photo(photo, caption=caption)
    await message.answer(link)


async def _handle_broadcast_text(message: types.Message) -> bool:
    admin_id = message.from_user.id if message.from_user else None
    if not admin_id or admin_id not in pending_broadcast:
        return False

    started_at = pending_broadcast.pop(admin_id)
    if not message.text:
        pending_broadcast[admin_id] = datetime.now(UTC)
        await message.answer(
            "Можно отправлять только текстовые сообщения для рассылки.",
            reply_markup=_default_user_keyboard(),
        )
        return True
    if message.text == "/cancel":
        await message.answer("🚫 Рассылка отменена.", reply_markup=_default_user_keyboard())
        return True

    if datetime.now(UTC) - started_at > BROADCAST_TIMEOUT:
        await message.answer(
            "⌛ Время ожидания истекло. Отправьте /broadcast, чтобы начать заново.",
            reply_markup=_default_user_keyboard(),
        )
        return True

    users = get_all_active_users()
    sent = 0
    for user_id, *_ in users:
        try:
            await bot.send_message(user_id, message.text)
            sent += 1
            await asyncio.sleep(0.2)
        except Exception:
            continue

    await message.answer(
        f"✅ Сообщение отправлено {sent} пользователям.",
        reply_markup=_default_user_keyboard(),
    )
    return True


async def _generate_ai_reply(message: types.Message) -> str:
    if not message.text:
        return "Я могу отвечать только на текстовые сообщения."

    history = get_last_messages(message.from_user.id, limit=5)
    model = GPT_ASSISTANT_ID or "gpt-4o-mini"

    prompt_messages = [
        {
            "role": "system",
            "content": (
                "Ты — AI-помощник сервиса VPN GPT. Помоги пользователю подобрать VPN,"
                " объясни преимущества, помоги с настройкой и сопровождением."
            ),
        }
    ]
    for previous_message, previous_reply in history:
        prompt_messages.append({"role": "user", "content": previous_message})
        prompt_messages.append({"role": "assistant", "content": previous_reply})
    prompt_messages.append({"role": "user", "content": message.text})

    if not client:
        return (
            "Сейчас сервис консультанта недоступен."
            " Попробуйте снова позже или используйте команду /buy для покупки VPN."
        )

    try:
        response = await client.chat.completions.create(
            model=model,
            messages=prompt_messages,
            temperature=0.7,
        )
    except Exception:
        return (
            "Произошла ошибка при обращении к модели."
            " Попробуйте повторить запрос чуть позже."
        )

    choices = getattr(response, "choices", None)
    if not choices:
        return "Я не получил ответа от модели. Попробуйте ещё раз."

    reply = choices[0].message.content if choices[0].message else None
    if not reply:
        return "Ответ модели пуст. Попробуйте переформулировать вопрос."
    return reply.strip()


async def _notify_expiring_users() -> None:
    if not ADMIN_ID:
        return

    while True:
        await asyncio.sleep(3600)
        expiring = get_expiring_keys(EXPIRING_THRESHOLD_DAYS)
        fresh: List[str] = []
        for user_id, name, expires_at in expiring:
            key = (user_id, expires_at)
            if key in notified_expiring:
                continue
            notified_expiring.add(key)
            formatted_name = name or str(user_id)
            fresh.append(
                f"• {formatted_name} — истекает {expires_at.strftime('%d.%m.%Y')}"
            )

        if fresh:
            text = "⏰ Подписки на исходе:\n" + "\n".join(fresh)
            try:
                await bot.send_message(ADMIN_ID, text)
            except Exception:
                pass


async def _handle_buy(message: types.Message) -> None:
    expires_at = datetime.now(UTC) + timedelta(days=DEFAULT_SUBSCRIPTION_DAYS)
    link = add_vpn_user()
    save_vpn_key(
        message.from_user.id,
        message.from_user.username if message.from_user else None,
        _format_name(message),
        link,
        expires_at,
    )
    await message.answer("⏳ Создаю подключение...")
    await _send_qr(message, link, expires_at)
    _log_interaction(
        message,
        "Выдано новое VPN-подключение. Ссылка: {link}. Доступ до {date}.".format(
            link=link, date=expires_at.strftime("%d.%m.%Y")
        ),
    )


async def _handle_renew(message: types.Message) -> None:
    new_expiration = renew_vpn_key(message.from_user.id, DEFAULT_SUBSCRIPTION_DAYS)
    if not new_expiration:
        await message.answer(
            "ℹ️ Активных подключений не найдено. Используйте команду /buy для покупки VPN.",
        )
        return
    reply = (
        "🔄 Подписка продлена!\n"
        f"Новый срок действия до {new_expiration.strftime('%d.%m.%Y')}."
    )
    await message.answer(reply)
    _log_interaction(message, reply)


def _log_interaction(message: types.Message, reply: str) -> None:
    save_message(
        message.from_user.id,
        message.from_user.username if message.from_user else None,
        _format_name(message),
        message.text or "",
        reply,
    )


@dp.message(Command("buy"))
async def buy(message: types.Message) -> None:
    await _handle_buy(message)


@dp.message(Command("renew"))
async def renew(message: types.Message) -> None:
    await _handle_renew(message)


@dp.message(Command("cancel"))
async def cancel(message: types.Message) -> None:
    if await _handle_broadcast_text(message):
        return
    await message.answer(
        "Нет активных действий для отмены.", reply_markup=_default_user_keyboard()
    )


@dp.message()
async def handle_message(message: types.Message) -> None:
    if await _handle_broadcast_text(message):
        return
    if not message.text:
        await message.answer(
            "Я могу обработать только текст. Попробуйте описать ваш вопрос словами."
        )
        return

    reply = await _generate_ai_reply(message)
    await message.answer(reply)
    _log_interaction(message, reply)


async def main():
    init_db()
    asyncio.create_task(_notify_expiring_users())
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())

