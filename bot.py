import os
import asyncio
import json
import uuid
import datetime
import sqlite3
import requests

from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.utils.keyboard import InlineKeyboardBuilder
from dotenv import load_dotenv
from openai import OpenAI

# -------------------- CONFIG --------------------
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID"))
MORUNE_API_KEY = os.getenv("MORUNE_API_KEY")
GPT_API_KEY = os.getenv("GPT_API_KEY")
GPT_ASSISTANT_ID = os.getenv("GPT_ASSISTANT_ID")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

DB_PATH = "dialogs.db"
XRAY_CONFIG = "/usr/local/etc/xray/config.json"
VPN_PORT = 2053


# -------------------- DATABASE --------------------
def init_db():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS vpn_keys (
            user_id INTEGER,
            username TEXT,
            uuid TEXT,
            issued_at TEXT,
            expires_at TEXT,
            active INTEGER
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS history (
            user_id INTEGER,
            message TEXT,
            role TEXT,
            ts TEXT
        )
    """)
    conn.commit()
    conn.close()


# -------------------- XRAY CONFIG --------------------
def add_xray_client(client_uuid, email):
    with open(XRAY_CONFIG, "r") as f:
        data = json.load(f)
    inbound = data["inbounds"][0]
    inbound["settings"]["clients"].append({
        "id": client_uuid,
        "level": 0,
        "email": email
    })
    with open(XRAY_CONFIG, "w") as f:
        json.dump(data, f, indent=2)
    os.system("systemctl restart xray")


def disable_xray_client(client_uuid):
    with open(XRAY_CONFIG, "r") as f:
        data = json.load(f)
    inbound = data["inbounds"][0]
    inbound["settings"]["clients"] = [
        c for c in inbound["settings"]["clients"] if c["id"] != client_uuid
    ]
    with open(XRAY_CONFIG, "w") as f:
        json.dump(data, f, indent=2)
    os.system("systemctl restart xray")


# -------------------- VPN KEYS --------------------
def add_vpn_key(user_id, username, days=30):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    new_uuid = str(uuid.uuid4())
    issued = datetime.datetime.now()
    expires = issued + datetime.timedelta(days=days)
    cur.execute("""
        INSERT INTO vpn_keys (user_id, username, uuid, issued_at, expires_at, active)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (user_id, username, new_uuid, issued.isoformat(), expires.isoformat(), 1))
    conn.commit()
    conn.close()
    add_xray_client(new_uuid, username)
    return new_uuid


# -------------------- MORUNE --------------------
def create_morune_payment(user_id: int, amount: float, description: str):
    headers = {"Authorization": f"Bearer {MORUNE_API_KEY}"}
    data = {
        "amount": amount,
        "currency": "RUB",
        "description": description,
        "callback_url": "https://yourdomain.com/morune_webhook",
        "metadata": {"user_id": user_id},
        "sandbox": False
    }
    r = requests.post("https://api.morune.com/v1/payments", json=data, headers=headers)
    r.raise_for_status()
    return r.json()["payment_url"]


# -------------------- ASSISTANT COMMUNICATION --------------------
def save_message(user_id: int, role: str, message: str):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO history (user_id, message, role, ts) VALUES (?, ?, ?, ?)",
        (user_id, message, role, datetime.datetime.now().isoformat())
    )
    conn.commit()
    conn.close()


async def ask_assistant(user_id: int, user_text: str) -> str:
    """Эмуляция ответов Assistant API без подключения к OpenAI"""
    save_message(user_id, "user", user_text.lower())

    text = user_text.lower()
    if "привет" in text:
        reply = "👋 Привет! Я — ваш AI VPN помощник. Помогу подобрать VPN под ваши нужды."
    elif "купить" in text or "цена" in text or "стоимость" in text:
        reply = "💳 Наш VPN стоит 300₽ за 30 дней. Хотите оплатить сейчас? Нажмите /buy."
    elif "настроить" in text:
        reply = "🧩 После покупки я отправлю вам ссылку и QR-код — просто отсканируйте в вашем VPN-клиенте."
    elif "помощ" in text or "support" in text:
        reply = "📞 Я всегда на связи. Опишите вашу проблему, и я помогу."
    else:
        reply = "🤖 Я пока в тестовом режиме без OpenAI, но готов помочь! Используйте команды /buy или /renew."

    save_message(user_id, "assistant", reply)
    return reply


# -------------------- COMMANDS --------------------
@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    await message.answer(
        "👋 Привет! Я — AI VPN Ассистент.\n\n"
        "Помогу подобрать, купить и настроить VPN-доступ.\n"
        "Выберите действие:\n\n"
        "👉 /buy — купить VPN на 30 дней\n"
        "🔁 /renew — продлить доступ\n"
        "💬 или просто напишите, что вам нужно."
    )


@dp.message(Command("buy"))
async def cmd_buy(message: types.Message):
    user_id = message.from_user.id
    username = message.from_user.username or "user"
    amount = 300
    description = "VPN-доступ на 30 дней"

    # Запоминаем клиента
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT 1 FROM vpn_keys WHERE user_id = ?", (user_id,))
    if not cur.fetchone():
        cur.execute(
            "INSERT INTO vpn_keys (user_id, username, uuid, issued_at, expires_at, active) VALUES (?, ?, '', '', '', 0)",
            (user_id, username)
        )
        conn.commit()
    conn.close()

    try:
        pay_url = create_morune_payment(user_id, amount, description)
        kb = InlineKeyboardBuilder()
        kb.row(types.InlineKeyboardButton(text=f"Оплатить {amount}₽", url=pay_url))
        await message.answer(
            "💳 Для получения доступа оплатите подписку:",
            reply_markup=kb.as_markup()
        )
    except Exception as e:
        await message.answer(f"Ошибка при создании платежа: {e}")


@dp.message(Command("renew"))
async def cmd_renew(message: types.Message):
    await message.answer("🔁 Функция продления скоро будет доступна.")


# -------------------- DEFAULT CHAT HANDLER --------------------
@dp.message()
async def handle_message(message: types.Message):
    user_id = message.from_user.id
    text = message.text.strip()
    reply = await ask_assistant(user_id, text)
    await message.answer(reply)


# -------------------- STARTUP --------------------
async def main():
    init_db()
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())

