import asyncio
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from config import BOT_TOKEN
from utils.vpn import add_vpn_user
from utils.qrgen import make_qr

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

@dp.message(Command("start"))
async def start(msg: types.Message):
    await msg.answer("👋 Привет! Я бот BusinessVPN.\nОтправь /buy чтобы получить подключение.")

@dp.message(Command("buy"))
async def buy(msg: types.Message):
    await msg.answer("⏳ Создаю подключение...")
    link = add_vpn_user()
    qr = make_qr(link)
    await msg.answer("✅ Ваш VPN готов!\nВот ссылка для подключения:")
    await msg.answer(link)
    await msg.answer_photo(qr, caption="📱 Отсканируйте QR-код для быстрого подключения")

async def main():
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())

