import requests
import os

MORUNE_API_KEY = os.getenv("MORUNE_API_KEY")
MORUNE_API_URL = "https://api.morune.com/v1/payments"

def create_payment(user_id: int, amount: float, description: str):
    """
    Создаёт платёж в Morune и возвращает ссылку на оплату.
    """
    headers = {"Authorization": f"Bearer {MORUNE_API_KEY}"}
    data = {
        "amount": amount,
        "currency": "RUB",
        "description": description,
        "callback_url": "https://yourdomain.com/morune_webhook",  # заменить на реальный адрес
        "metadata": {"user_id": user_id},
        "sandbox": False  # True для теста
    }

    r = requests.post(MORUNE_API_URL, json=data, headers=headers)
    r.raise_for_status()
    return r.json()["payment_url"]

