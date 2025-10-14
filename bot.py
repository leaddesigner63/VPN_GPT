from __future__ import annotations

import asyncio
import logging
import os
from collections import defaultdict, deque
from typing import Any, Deque, Dict
from urllib.parse import urlencode

import httpx
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
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

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
GPT_API_KEY = os.getenv("GPT_API_KEY")
GPT_MODEL = os.getenv("GPT_MODEL", "gpt-4o-mini")
SYSTEM_PROMPT = os.getenv(
    "GPT_SYSTEM_PROMPT",
    "Ты — VPN_GPT, эксперт по VPN. Отвечай дружелюбно, кратко и по делу.",
)
# shell-style inline комментарии в переменных окружения иногда приводят к тому,
# что стандартный ``int()`` не может преобразовать значение. Чтобы не падать при
# загрузке конфигурации, очищаем такие комментарии.


def _strip_inline_comment(raw: str) -> str:
    comment_pos = raw.find("#")
    if comment_pos == -1:
        return raw.strip()
    return raw[:comment_pos].strip()


def _get_int_env(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    cleaned = _strip_inline_comment(raw)
    if cleaned == "":
        return default
    try:
        return int(cleaned)
    except ValueError as exc:  # pragma: no cover - defensive guard
        raise RuntimeError(f"Переменная окружения {name} должна быть целым числом") from exc


MAX_HISTORY_MESSAGES = _get_int_env("GPT_HISTORY_MESSAGES", 6)
# FastAPI backend обслуживает бота на порту 8080 согласно документации.
# Ранее значение по умолчанию указывало на 8000, из-за чего при отсутствии
# переменной окружения бот безуспешно подключался к несуществующему сервису и
# падал с httpx.ConnectError. Для надёжности явно указываем IPv4-хост, чтобы
# избежать попыток соединения по IPv6, которые могут быть недоступны в проде.
VPN_API_URL = os.getenv("VPN_API_URL", "http://127.0.0.1:8080")
SERVICE_TOKEN = os.getenv("INTERNAL_TOKEN") or os.getenv("ADMIN_TOKEN", "")
BOT_PAYMENT_URL = os.getenv("BOT_PAYMENT_URL", "https://vpn-gpt.store/payment.html").rstrip("/")
TRIAL_DAYS = _get_int_env("TRIAL_DAYS", 0)
PLAN_ENV = os.getenv("PLANS", "1m:180,3m:460,12m:1450")
REFERRAL_BONUS_DAYS = _get_int_env("REFERRAL_BONUS_DAYS", 30)

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is not configured")
if not GPT_API_KEY:
    raise RuntimeError("GPT_API_KEY is not configured")

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("vpn_gpt.bot")


def _parse_plans(raw: str) -> Dict[str, int]:
    plans: Dict[str, int] = {}
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if ":" not in chunk:
            continue
        code, price = chunk.split(":", 1)
        try:
            plans[code.strip()] = int(price.strip())
        except ValueError:
            logger.warning("Invalid plan price", extra={"plan": chunk})
    return plans or {"1m": 180, "3m": 450, "12m": 1450}


PLANS = _parse_plans(PLAN_ENV)
PLAN_ORDER = [code for code in ("1m", "3m", "12m") if code in PLANS] + [
    code for code in PLANS.keys() if code not in {"1m", "3m", "12m"}
]

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher(storage=MemoryStorage())
client = OpenAI(api_key=GPT_API_KEY)


class _QrMessageTracker:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._messages: dict[int, int] = {}

    async def remember(self, chat_id: int, message_id: int) -> None:
        async with self._lock:
            self._messages[chat_id] = message_id

    async def pop(self, chat_id: int) -> int | None:
        async with self._lock:
            return self._messages.pop(chat_id, None)


_qr_messages = _QrMessageTracker()


async def _delete_previous_qr(chat_id: int) -> None:
    message_id = await _qr_messages.pop(chat_id)
    if message_id is None:
        return
    try:
        await bot.delete_message(chat_id, message_id)
    except Exception:
        logger.debug(
            "Failed to delete previous QR message",
            extra={"chat_id": chat_id, "message_id": message_id},
        )


class AiFlow(StatesGroup):
    device = State()
    goal = State()
    priority = State()


ConversationHistory = Deque[dict[str, str]]
_histories: Dict[int, ConversationHistory] = defaultdict(
    lambda: deque(maxlen=MAX_HISTORY_MESSAGES * 2 if MAX_HISTORY_MESSAGES > 0 else None)
)
BOT_USERNAME: str | None = None


MENU_QUICK = "menu_quick"
MENU_AI = "menu_ai"
MENU_KEYS = "menu_keys"
MENU_PAY = "menu_pay"
MENU_REF = "menu_ref"
MENU_HELP = "menu_help"
MENU_BACK = "menu_back"
PAY_PLAN_PREFIX = "pay_plan:"
CANCEL_AI = "ai_cancel"


def build_main_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🚀 Быстрый старт", callback_data=MENU_QUICK)],
            [InlineKeyboardButton(text="🧠 Подобрать с AI", callback_data=MENU_AI)],
            [InlineKeyboardButton(text="🔑 Мои ключи", callback_data=MENU_KEYS)],
            [InlineKeyboardButton(text="💳 Оплатить", callback_data=MENU_PAY)],
            [InlineKeyboardButton(text="🤝 Рефералы", callback_data=MENU_REF)],
            [InlineKeyboardButton(text="ℹ️ Помощь", callback_data=MENU_HELP)],
        ]
    )


def build_back_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="⬅️ Главное меню", callback_data=MENU_BACK)]]
    )


def build_payment_keyboard(username: str, chat_id: int | None, ref: str | None) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for plan in PLAN_ORDER:
        price = PLANS[plan]
        params = {"u": username, "plan": plan}
        if chat_id:
            params["c"] = str(chat_id)
        if ref:
            params["r"] = ref
        payment_url = f"{BOT_PAYMENT_URL}?{urlencode(params)}"
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"{plan.upper()} · {price} ₽",
                    url=payment_url,
                )
            ]
        )
    rows.append([InlineKeyboardButton(text="⬅️ Главное меню", callback_data=MENU_BACK)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _is_supported_button_link(link: str) -> bool:
    if not link:
        return False
    return link.startswith("http") or link.startswith("tg://")


def build_result_markup(link: str | None = None) -> InlineKeyboardMarkup:
    buttons: list[list[InlineKeyboardButton]] = []
    if link and _is_supported_button_link(link):
        buttons.append([InlineKeyboardButton(text="🔗 Открыть ссылку", url=link)])
    buttons.append([InlineKeyboardButton(text="⬅️ Главное меню", callback_data=MENU_BACK)])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def _get_history(chat_id: int) -> ConversationHistory:
    return _histories[chat_id]


def _remember_exchange(chat_id: int, user_text: str, reply: str) -> None:
    history = _get_history(chat_id)
    history.append({"role": "user", "content": user_text})
    history.append({"role": "assistant", "content": reply})


def _build_messages(chat_id: int, user_text: str) -> list[dict[str, str]]:
    history = list(_get_history(chat_id))
    messages: list[dict[str, str]] = []
    if SYSTEM_PROMPT:
        messages.append({"role": "system", "content": SYSTEM_PROMPT})
    messages.extend(history)
    messages.append({"role": "user", "content": user_text})
    return messages


async def ask_gpt(chat_id: int, user_text: str) -> str:
    messages = _build_messages(chat_id, user_text)
    completion = await asyncio.get_event_loop().run_in_executor(
        None,
        lambda: client.chat.completions.create(model=GPT_MODEL, messages=messages),
    )
    reply = completion.choices[0].message.content or ""
    _remember_exchange(chat_id, user_text, reply)
    return reply


async def api_post(path: str, payload: dict[str, Any]) -> dict[str, Any]:
    headers = {"Authorization": f"Bearer {SERVICE_TOKEN}"} if SERVICE_TOKEN else {}
    async with httpx.AsyncClient(timeout=15.0) as http_client:
        response = await http_client.post(
            f"{VPN_API_URL.rstrip('/')}{path}", json=payload, headers=headers
        )
    response.raise_for_status()
    return response.json()


async def api_get(path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    headers = {"Authorization": f"Bearer {SERVICE_TOKEN}"} if SERVICE_TOKEN else {}
    async with httpx.AsyncClient(timeout=15.0) as http_client:
        response = await http_client.get(
            f"{VPN_API_URL.rstrip('/')}{path}", params=params, headers=headers
        )
    response.raise_for_status()
    return response.json()


async def register_user(username: str, chat_id: int, ref: str | None) -> None:
    try:
        await api_post("/users/register", {"username": username, "chat_id": chat_id, "referrer": ref})
    except httpx.HTTPStatusError as exc:
        logger.warning("Failed to register user", extra={"status": exc.response.status_code})


async def apply_referral(referrer: str, referee: str, chat_id: int) -> None:
    try:
        await api_post(
            "/referral/use",
            {"referrer": referrer, "referee": referee, "chat_id": chat_id},
        )
    except httpx.HTTPStatusError as exc:
        logger.info("Referral not applied", extra={"status": exc.response.status_code})


async def issue_trial_key(username: str, chat_id: int) -> dict[str, Any] | None:
    try:
        payload = await api_post(
            "/vpn/issue_key",
            {"username": username, "chat_id": chat_id, "trial": True},
        )
        if not payload.get("ok"):
            return None
        return payload
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 409:
            return exc.response.json()
        if exc.response.status_code == 503:
            try:
                error_body = exc.response.json()
            except ValueError:  # pragma: no cover - defensive
                error_body = {"detail": exc.response.text}
            detail = error_body.get("error") or error_body.get("detail")
            if detail == "service_token_not_configured":
                logger.error("VPN API is unavailable: service token is not configured")
                return {"ok": False, "error": "service_unavailable"}
        logger.exception("Failed to issue key")
        return None


async def fetch_keys(username: str) -> list[dict[str, Any]]:
    try:
        response = await api_get(f"/users/{username}/keys")
        if response.get("ok"):
            return response.get("keys", [])
    except httpx.HTTPStatusError as exc:
        logger.exception("Failed to fetch keys", extra={"status": exc.response.status_code})
    return []


async def fetch_referral_stats(username: str) -> dict[str, Any]:
    try:
        response = await api_get(f"/users/{username}/referrals")
        if response.get("ok"):
            return response
    except httpx.HTTPStatusError:
        pass
    return {"username": username, "total_referrals": 0, "total_days": 0}


def format_key_message(payload: dict[str, Any]) -> str:
    expires = payload.get("expires_at", "—")
    trial = "да" if payload.get("trial") else "нет"
    status = "активен" if payload.get("active") else "неактивен"
    parts = [
        "<b>VPN-ключ</b>",
        f"UUID: <code>{payload.get('uuid')}</code>",
        f"Статус: {status}",
        f"Триал: {trial}",
        f"Действует до: {expires}",
    ]
    link = payload.get("link")
    if link:
        parts.append("")
        parts.append(f"<code>{link}</code>")
    return "\n".join(parts)


def build_ai_instruction_prompt(device: str, goal: str, priority: str, trial_days: int, plans: Dict[str, int]) -> str:
    plan_parts = [f"{code.upper()} — {price} ₽" for code, price in plans.items()]
    return (
        "Ты помогаешь пользователю настроить VPN. Сформируй короткую памятку из 3-4 пунктов: "
        "1) какую программу установить под устройство, 2) как импортировать ссылку VLESS, 3) как оплатить тариф. "
        "Пиши дружелюбно, без жаргона, используй эмодзи экономно.\n"
        f"Устройство: {device}.\nЦель: {goal}.\nПриоритет: {priority}.\n"
        f"Триал: {trial_days} дней. Тарифы: {', '.join(plan_parts)}."
    )


def build_ai_keyboard(link: str | None, username: str, chat_id: int, ref: str | None) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if link and _is_supported_button_link(link):
        rows.append([InlineKeyboardButton(text="📥 Импортировать", url=link)])
    rows.append([InlineKeyboardButton(text="💳 Оплатить", callback_data=MENU_PAY)])
    rows.append([InlineKeyboardButton(text="⬅️ Главное меню", callback_data=MENU_BACK)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def build_help_text() -> str:
    return (
        "ℹ️ <b>Нужна помощь?</b>\n"
        "1. Установи V2Box на iOS/Android или Nekobox на Windows/macOS.\n"
        "2. Импортируй ссылку VLESS из карточки ключа.\n"
        "3. Если что-то не получается — напиши в чат поддержки @dobriy_vpn_support."
    )


@dp.message(CommandStart())
async def handle_start(message: Message, state: FSMContext) -> None:
    await _delete_previous_qr(message.chat.id)
    await state.clear()
    user = message.from_user
    if user is None:
        return
    username = user.username or f"id_{user.id}"
    payload = ""
    if message.text and " " in message.text:
        payload = message.text.split(" ", 1)[1]
    ref = payload.strip() or None

    if ref and ref != username:
        await apply_referral(ref, username, message.chat.id)

    await register_user(username, message.chat.id, ref)
    await bot.set_chat_menu_button(message.chat.id, MenuButtonDefault())

    greeting = (
        "👋 Привет! Я VPN_GPT — помогу подключиться к VPN в три шага:\n"
        "1️⃣ Получи ключ (тест на 3 дня).\n"
        "2️⃣ Следуй инструкции, подключи приложение.\n"
        "3️⃣ Оплати подходящий тариф — и пользуйся без ограничений."
    )
    await message.answer(greeting, reply_markup=build_main_menu())


@dp.callback_query(F.data == MENU_BACK)
async def handle_menu_back(call: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    if call.message:
        await _delete_previous_qr(call.message.chat.id)
    await call.message.edit_text("Выбери действие:", reply_markup=build_main_menu())
    await call.answer()


@dp.callback_query(F.data == MENU_QUICK)
async def handle_quick_start(call: CallbackQuery) -> None:
    user = call.from_user
    if user is None:
        await call.answer()
        return
    if call.message:
        await _delete_previous_qr(call.message.chat.id)
    username = user.username or f"id_{user.id}"
    await register_user(username, call.message.chat.id, None)
    payload = await issue_trial_key(username, call.message.chat.id)
    if not payload:
        await call.message.edit_text(
            "⚠️ Не удалось выдать ключ. Попробуй позже или свяжись с поддержкой.",
            reply_markup=build_back_menu(),
        )
        await call.answer()
        return

    if payload.get("error") == "service_unavailable":
        await call.message.edit_text(
            "😔 Сейчас не удаётся выдать ключи — сервис недоступен. "
            "Мы уже работаем над решением. Попробуй позже или напиши в поддержку.",
            reply_markup=build_back_menu(),
        )
        await call.answer()
        return

    if payload.get("error") == "trial_already_used":
        await call.message.edit_text(
            "У тебя уже есть активный тестовый ключ. Посмотри его в разделе «Мои ключи».",
            reply_markup=build_back_menu(),
        )
        await call.answer()
        return

    link = payload.get("link")
    text = (
        "🎁 Готово! Твой тестовый доступ активирован."\
        + "\n\n" + format_key_message(payload)
    )
    await call.message.edit_text(text, reply_markup=build_result_markup(link))
    if link:
        qr = make_qr(link)
        qr_message = await call.message.answer_photo(
            BufferedInputFile(qr.getvalue(), filename="vpn_key.png"),
            caption="📱 Отсканируй, чтобы добавить ключ в приложение",
        )
        await _qr_messages.remember(call.message.chat.id, qr_message.message_id)
    await call.answer("Ключ выдан")


@dp.callback_query(F.data == MENU_KEYS)
async def handle_my_keys(call: CallbackQuery) -> None:
    user = call.from_user
    if user is None:
        await call.answer()
        return
    if call.message:
        await _delete_previous_qr(call.message.chat.id)
    username = user.username or f"id_{user.id}"
    keys = await fetch_keys(username)
    if not keys:
        text = "Пока что ключей нет. Нажми «Быстрый старт», чтобы получить тестовый доступ!"
    else:
        parts = ["🔑 <b>Твои ключи</b>"]
        for idx, key in enumerate(keys, start=1):
            status = "✅ активен" if key.get("active") else "⚠️ неактивен"
            parts.append(
                f"\n<b>#{idx}</b> · {status}\nДействует до: {key.get('expires_at', '—')}"
            )
            if key.get("link"):
                parts.append(f"<code>{key['link']}</code>")
        text = "\n".join(parts)
    reply_markup = build_payment_keyboard(username, call.message.chat.id, username)
    await call.message.edit_text(text, reply_markup=reply_markup)
    await call.answer()


@dp.callback_query(F.data == MENU_PAY)
async def handle_pay(call: CallbackQuery) -> None:
    user = call.from_user
    if user is None:
        await call.answer()
        return
    if call.message:
        await _delete_previous_qr(call.message.chat.id)
    username = user.username or f"id_{user.id}"
    text = "Выбери тариф: оплата откроется в браузере на сайте vpn-gpt.store."
    keyboard = build_payment_keyboard(username, call.message.chat.id, username)
    await call.message.edit_text(text, reply_markup=keyboard)
    await call.answer()


@dp.callback_query(F.data == MENU_REF)
async def handle_referrals(call: CallbackQuery) -> None:
    user = call.from_user
    if user is None:
        await call.answer()
        return
    if call.message:
        await _delete_previous_qr(call.message.chat.id)
    username = user.username or f"id_{user.id}"
    stats = await fetch_referral_stats(username)
    ref_link = f"https://t.me/{BOT_USERNAME}?start={username}" if BOT_USERNAME else ""
    text = (
        "🤝 <b>Реферальная программа</b>\n"
        f"Пригласи друга — и после его оплаты получи +{REFERRAL_BONUS_DAYS} дней.\n\n"
        f"Твой прогресс: {stats.get('total_referrals', 0)} приглашений, {stats.get('total_days', 0)} бонусных дней.\n"
        f"Ссылка: {ref_link or 'поделись своим @username'}"
    )
    await call.message.edit_text(text, reply_markup=build_back_menu())
    await call.answer()


@dp.callback_query(F.data == MENU_HELP)
async def handle_help(call: CallbackQuery) -> None:
    if call.message:
        await _delete_previous_qr(call.message.chat.id)
    await call.message.edit_text(build_help_text(), reply_markup=build_back_menu())
    await call.answer()


@dp.callback_query(F.data == MENU_AI)
async def handle_ai_start(call: CallbackQuery, state: FSMContext) -> None:
    if call.message:
        await _delete_previous_qr(call.message.chat.id)
    await state.set_state(AiFlow.device)
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data=CANCEL_AI)]]
    )
    await call.message.edit_text(
        "🧠 Давай подберём оптимальный сценарий. Какое устройство хочешь подключить?",
        reply_markup=keyboard,
    )
    await call.answer()


@dp.callback_query(F.data == CANCEL_AI)
async def handle_ai_cancel(call: CallbackQuery, state: FSMContext) -> None:
    if call.message:
        await _delete_previous_qr(call.message.chat.id)
    await state.clear()
    await call.message.edit_text("Ок! Возвращаемся в меню.", reply_markup=build_main_menu())
    await call.answer()


@dp.message(AiFlow.device)
async def process_ai_device(message: Message, state: FSMContext) -> None:
    await _delete_previous_qr(message.chat.id)
    await state.update_data(device=message.text.strip())
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data=CANCEL_AI)]]
    )
    await message.answer("Отлично! Для чего нужен VPN (стриминг, соцсети, безопасность)?", reply_markup=keyboard)
    await state.set_state(AiFlow.goal)


@dp.message(AiFlow.goal)
async def process_ai_goal(message: Message, state: FSMContext) -> None:
    await _delete_previous_qr(message.chat.id)
    await state.update_data(goal=message.text.strip())
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data=CANCEL_AI)]]
    )
    await message.answer("Что важнее всего: скорость, стабильность или обход блокировок?", reply_markup=keyboard)
    await state.set_state(AiFlow.priority)


@dp.message(AiFlow.priority)
async def process_ai_priority(message: Message, state: FSMContext) -> None:
    await _delete_previous_qr(message.chat.id)
    data = await state.get_data()
    device = data.get("device", "устройство не указано")
    goal = data.get("goal", "цель не указана")
    priority = message.text.strip()
    await state.clear()

    user = message.from_user
    if user is None:
        return
    username = user.username or f"id_{user.id}"
    await register_user(username, message.chat.id, None)

    trial_payload = await issue_trial_key(username, message.chat.id)
    if trial_payload and trial_payload.get("error") == "trial_already_used":
        trial_payload = None

    link = trial_payload.get("link") if trial_payload else None

    prompt = build_ai_instruction_prompt(device, goal, priority, TRIAL_DAYS, PLANS)
    ai_message = await ask_gpt(message.chat.id, prompt)

    response_parts = ["🧠 <b>Твой персональный план</b>", ai_message.strip()]
    if trial_payload:
        response_parts.append("\n🎁 Тестовый доступ уже активирован:")
        response_parts.append(format_key_message(trial_payload))
    else:
        response_parts.append(
            "\nУ тебя уже есть активный ключ. Посмотри его в разделе «Мои ключи»."
        )

    keyboard = build_ai_keyboard(link, username, message.chat.id, user.username)
    await message.answer("\n".join(response_parts), reply_markup=keyboard)

    if link:
        qr = make_qr(link)
        qr_message = await message.answer_photo(
            BufferedInputFile(qr.getvalue(), filename="vpn_key.png"),
            caption="📱 Отсканируй QR для быстрого подключения",
        )
        await _qr_messages.remember(message.chat.id, qr_message.message_id)


@dp.message(Command("help"))
async def command_help(message: Message):
    await _delete_previous_qr(message.chat.id)
    await message.answer(build_help_text(), reply_markup=build_back_menu())


@dp.message()
async def handle_message(message: Message) -> None:
    await _delete_previous_qr(message.chat.id)
    user = message.from_user
    if user is None or not message.text:
        return
    reply = await ask_gpt(message.chat.id, message.text)
    await message.answer(reply, reply_markup=build_back_menu())


async def on_startup() -> None:
    global BOT_USERNAME
    me = await bot.get_me()
    BOT_USERNAME = me.username
    logger.info("Bot started", extra={"username": BOT_USERNAME})


async def main() -> None:
    await on_startup()
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
