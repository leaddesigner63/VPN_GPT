import asyncio
import json
from datetime import datetime, timedelta
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from openai import AsyncOpenAI
from config import BOT_TOKEN, GPT_API_KEY, GPT_ASSISTANT_ID, ADMIN_ID
from utils.vpn import add_vpn_user
from utils.qrgen import make_qr
from utils.db import (
    init_db, save_message, get_last_messages,
    save_vpn_key, get_expiring_keys, renew_vpn_key,
    get_expired_keys, deactivate_vpn_key, get_all_active_users
)
import subprocess
import os

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
client = AsyncOpenAI(api_key=GPT_API_KEY)
XRAY_CONFIG = "/usr/local/etc/xray/config.json"


# === Проверка прав ===
def is_admin(user_id):
    return user_id == ADMIN_ID


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

    keyboard = types.ReplyKeyboardMarkup(resize_keyboard=True)
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
        text += f"• {name} — до {exp[:10]} (ID: {uid})\n"
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
        text += f"• {full_name} (ID: {user_id})\n"
    await msg.answer(text)


# === Рассылка ===
@dp.message(Command("broadcast"))
async def broadcast(msg: types.Message):
    if not is_admin(msg.from_user.id):
        return await msg.answer("⛔ Доступ запрещён")

    await msg.answer("📢 Введите текст рассылки:")
    dp.message.register(send_broadcast)


async def send_broadcast(msg: types.Message):
    if not is_admin(msg.from_user.id):
        return

    users = get_all_active_users()
    text = msg.text
    count = 0
    for u in users:
        try:
            await bot.send_message(u[0], text)
            count += 1
            await asyncio.sleep(0.2)
        except:
            pass
    await msg.answer(f"✅ Сообщение отправлено {count} пользователям.")
    dp.message.unregister(send_broadcast)


# === Остальные функции (buy, renew, GPT, мониторинг) — остаются без изменений ===
# ... (вставь сюда код с предыдущей версии bot.py)


async def main():
    init_db()
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())

