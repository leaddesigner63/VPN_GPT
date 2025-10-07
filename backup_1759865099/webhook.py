from fastapi import FastAPI, Request
from bot import add_vpn_key, bot
import logging
import asyncio
import qrcode
from io import BytesIO

app = FastAPI()
logger = logging.getLogger("morune_webhook")

SERVER_IP = "45.92.174.166"
PORT = 2053


@app.post("/morune_webhook")
async def morune_webhook(request: Request):
    data = await request.json()
    logger.info(f"Webhook data: {data}")

    if data.get("status") == "paid":
        user_id = data["metadata"].get("user_id")
        username = f"user_{user_id}"

        try:
            # создаём VPN-ключ
            uuid = add_vpn_key(user_id, username)
            link = f"vless://{uuid}@{SERVER_IP}:{PORT}?security=none&encryption=none#VPN_AI"

            # создаём QR-код
            qr_img = qrcode.make(link)
            bio = BytesIO()
            qr_img.save(bio, format="PNG")
            bio.seek(0)

            # отправляем сообщение пользователю
            msg = (
                "✅ Оплата получена!\n\n"
                f"Ваш VPN активен на 30 дней.\n\n"
                "🔗 Подключение:\n"
                f"`{link}`"
            )

            await bot.send_message(chat_id=user_id, text=msg, parse_mode="Markdown")
            await bot.send_photo(chat_id=user_id, photo=bio, caption="📱 Отсканируйте QR-код для подключения")

            logger.info(f"VPN key activated and message with QR sent to user {user_id}")

        except Exception as e:
            logger.error(f"Ошибка при активации VPN: {e}")

    return {"ok": True}

