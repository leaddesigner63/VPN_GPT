from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import UTC, datetime
from collections import defaultdict, deque
from pathlib import Path
from typing import Any, Awaitable, Callable, Deque, Dict, Sequence
from urllib.parse import urlencode, urlparse

import httpx
from aiogram import BaseMiddleware, Bot, Dispatcher, F
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
    LabeledPrice,
    MenuButtonDefault,
    Message,
    PreCheckoutQuery,
)
from dotenv import load_dotenv
from openai import OpenAI

from api.utils import db as db_utils
from handlers.stars import (
    StarHandlerDependencies,
    process_pending_deliveries as process_pending_star_deliveries,
    setup_stars_handlers,
)
from utils.content_filters import assert_no_geoblocking, sanitize_text
from utils.qrgen import make_qr
from utils.stars import StarPlan, StarSettings, build_invoice_payload, load_star_settings, resolve_plan_duration

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
GPT_API_KEY = os.getenv("GPT_API_KEY")
GPT_MODEL = os.getenv("GPT_MODEL", "gpt-4o-mini")

_SYSTEM_PROMPTS_PATH = Path(__file__).resolve().parent / "system_prompts.json"


def _load_system_prompts() -> list[str]:
    env_prompt = os.getenv("GPT_SYSTEM_PROMPT")
    if env_prompt:
        stripped = env_prompt.strip()
        return [stripped] if stripped else []

    try:
        raw = _SYSTEM_PROMPTS_PATH.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        stripped = raw.strip()
        return [stripped] if stripped else []

    if isinstance(data, str):
        stripped = data.strip()
        return [stripped] if stripped else []

    if isinstance(data, list):
        prompts: list[str] = []
        for item in data:
            if isinstance(item, str):
                stripped = item.strip()
                if stripped:
                    prompts.append(stripped)
        return prompts

    return []


SYSTEM_PROMPTS = _load_system_prompts() or [
    "Ты — VPN_GPT, эксперт по VPN. Отвечай дружелюбно, кратко и по делу.",
]
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


def _get_float_env(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None:
        return default
    cleaned = _strip_inline_comment(raw)
    if cleaned == "":
        return default
    try:
        return float(cleaned)
    except ValueError as exc:  # pragma: no cover - defensive guard
        raise RuntimeError(f"Переменная окружения {name} должна быть числом") from exc


MAX_HISTORY_MESSAGES = _get_int_env("GPT_HISTORY_MESSAGES", 6)
# FastAPI backend обслуживает бота на порту 8080 согласно документации.
# Ранее значение по умолчанию указывало на 8000, из-за чего при отсутствии
# переменной окружения бот безуспешно подключался к несуществующему сервису и
# падал с httpx.ConnectError. Для надёжности явно указываем IPv4-хост, чтобы
# избежать попыток соединения по IPv6, которые могут быть недоступны в проде.
VPN_API_URL = os.getenv("VPN_API_URL", "http://127.0.0.1:8080")
SERVICE_TOKEN = os.getenv("INTERNAL_TOKEN") or os.getenv("ADMIN_TOKEN", "")
BOT_PAYMENT_URL = os.getenv("BOT_PAYMENT_URL", "https://vpn-gpt.store/payment.html").rstrip("/")
PLAN_ENV = os.getenv("PLANS", "1m:80,3m:200,1y:700")
TEST_PLAN_CODE = os.getenv("STARS_TEST_PLAN_CODE", "test_1d")


def _parse_admin_usernames(raw: str | None) -> set[str]:
    if not raw:
        return set()
    parts = [chunk.strip().lstrip("@") for chunk in raw.split(",")]
    return {part.lower() for part in parts if part}


BOT_ADMIN_USERNAMES = _parse_admin_usernames(os.getenv("BOT_ADMINS"))
REFERRAL_BONUS_DAYS = _get_int_env("REFERRAL_BONUS_DAYS", 30)
API_TIMEOUT = _get_float_env("VPN_API_TIMEOUT", 15.0)
API_MAX_RETRIES = max(1, _get_int_env("VPN_API_MAX_RETRIES", 3))
API_RETRY_BASE_DELAY = _get_float_env("VPN_API_RETRY_BASE_DELAY", 0.5)

_VLESS_CLIENTS_RECOMMENDATIONS_PATH = Path(__file__).resolve().parent / "VLESS_clients_recommendations_ru.txt"
_DEFAULT_VLESS_CLIENTS_RECOMMENDATIONS = (
    "• Android — <a href=\"https://play.google.com/store/apps/details?id=com.v2ray.ang\">v2rayNG</a>\n"
    "• iOS — <a href=\"https://apps.apple.com/app/stash-rule-based-proxy/id1596063349\">Stash</a>\n"
    "• Windows — <a href=\"https://apps.microsoft.com/store/detail/v2rayn/9NKBQF3F8K6H\">v2rayN</a>\n"
    "• macOS — <a href=\"https://apps.apple.com/app/stash-rule-based-proxy/id1596063349\">Stash</a>\n"
    "• Linux — <a href=\"https://github.com/v2rayA/v2rayA\">v2rayA</a>"
)

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is not configured")
if not GPT_API_KEY:
    raise RuntimeError("GPT_API_KEY is not configured")

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("vpn_gpt.bot")


def _load_vless_clients_recommendations() -> str:
    try:
        content = _VLESS_CLIENTS_RECOMMENDATIONS_PATH.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        logger.warning(
            "VLESS clients recommendations file is missing",
            extra={"path": str(_VLESS_CLIENTS_RECOMMENDATIONS_PATH)},
        )
        return _DEFAULT_VLESS_CLIENTS_RECOMMENDATIONS

    if not content:
        logger.warning(
            "VLESS clients recommendations file is empty",
            extra={"path": str(_VLESS_CLIENTS_RECOMMENDATIONS_PATH)},
        )
        return _DEFAULT_VLESS_CLIENTS_RECOMMENDATIONS

    return content


def _format_vless_clients_recommendations(indent: str = "") -> str:
    lines = _VLESS_CLIENTS_RECOMMENDATIONS.splitlines()
    return "\n".join(f"{indent}{line}" if line else "" for line in lines)


_VLESS_CLIENTS_RECOMMENDATIONS = _load_vless_clients_recommendations()
_VLESS_CLIENTS_SYSTEM_PROMPT = (
    "Список рекомендованных приложений для подключения по протоколу VLESS. "
    "Выбирай варианты, которые подходят под устройство пользователя, и не перечисляй лишние. "
    "Для каждой операционной системы используй не более одного приложения, не добавляй описания и комментарии. "
    "Применяй формат «• ОС — <a href=\"URL\">Название</a>» без отображения голых ссылок.\n"
    f"{_VLESS_CLIENTS_RECOMMENDATIONS}"
)


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
    return plans or {"1m": 80, "3m": 200, "1y": 700}


PLANS = _parse_plans(PLAN_ENV)
PLAN_ORDER = [code for code in ("1m", "3m", "1y", "12m") if code in PLANS] + [
    code for code in PLANS.keys() if code not in {"1m", "3m", "1y", "12m"}
]

PLAN_DISPLAY_LABELS = {
    "1m": "1 месяц",
    "3m": "3 месяца",
    "1y": "12 месяцев",
    "12m": "12 месяцев",
}

STAR_SETTINGS: StarSettings = load_star_settings()
STAR_PAY_PREFIX = "stars:buy:"


def _get_star_plan(code: str) -> StarPlan | None:
    if not code:
        return None
    return STAR_SETTINGS.plans.get(code)


def _format_subscription_period_label(duration_days: int) -> str:
    if duration_days == 30:
        return "мес"
    if duration_days == 90:
        return "3 мес"
    if duration_days == 180:
        return "6 мес"
    if duration_days in (360, 365):
        return "год"
    return f"{duration_days} дн"


def _format_star_plan_button(plan: StarPlan) -> str:
    if plan.is_subscription:
        period_label = _format_subscription_period_label(plan.duration_days)
        return f"⭐️ {plan.title} подписка · {plan.price_stars}⭐/{period_label}"
    return f"⭐️ {plan.title} · {plan.price_stars}⭐"


def _ordered_star_plans(settings: StarSettings) -> list[StarPlan]:
    preferred = [code for code in (TEST_PLAN_CODE, "1m", "3m", "1y", "12m") if code]
    seen: set[str] = set()
    ordered: list[StarPlan] = []
    for code in preferred:
        plan = settings.plans.get(code)
        if plan and plan.code not in seen:
            ordered.append(plan)
            seen.add(plan.code)
    for plan in settings.available_plans():
        if plan.code in seen:
            continue
        ordered.append(plan)
        seen.add(plan.code)
    return ordered


def _format_active_key_quick_start_message(active_keys: Sequence[dict[str, Any]]) -> str:
    if not active_keys:
        raise ValueError("active_keys must not be empty")

    key = active_keys[0]
    expires_at = key.get("expires_at") or "—"
    subscription_key = _find_active_subscription_key(active_keys)

    lines = [
        "🔐 <b>Доступ уже активен</b>",
        f"Текущий ключ действует до: {expires_at}",
        "",
    ]
    if subscription_key:
        lines.append(
            "Подписка обновляется автоматически — я напомню, если возникнут проблемы с оплатой."
        )
        lines.append(
            "Управлять подпиской можно через «🔑 Мои ключи» или настройки Telegram."
        )
    else:
        lines.append(
            "Продли доступ по действующим тарифам — выбери подходящий вариант ниже."
        )
        lines.append("Если понадобится ключ ещё раз, открой раздел «🔑 Мои ключи».")
    return "\n".join(lines)


async def ensure_star_deliveries(message: Message, username: str) -> None:
    if not STAR_SETTINGS.enabled:
        return
    try:
        await process_pending_star_deliveries(message, username)
    except Exception as exc:  # pragma: no cover - defensive logging
        logger.exception(
            "Failed to process pending Stars deliveries",
            extra={"username": username, "chat_id": message.chat.id, "error": str(exc)},
        )


def _is_admin_user(user: Message | CallbackQuery | None, *, from_user=None) -> bool:
    target = from_user or (user.from_user if user else None)
    if target is None:
        return False
    if not BOT_ADMIN_USERNAMES:
        return False
    username = target.username
    if not username:
        return False
    return username.lower() in BOT_ADMIN_USERNAMES


bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher(storage=MemoryStorage())
client = OpenAI(api_key=GPT_API_KEY)


class VpnApiUnavailableError(RuntimeError):
    """Raised when the VPN API consistently returns server-side errors."""

    def __init__(self, status_code: int | None = None) -> None:
        self.status_code = status_code
        message = "VPN API недоступно"
        if status_code is not None:
            message = f"VPN API недоступно (HTTP {status_code})"
        super().__init__(message)


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


class _QrLinkStorage:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._links: dict[int, str] = {}

    async def remember(self, chat_id: int, link: str) -> None:
        async with self._lock:
            self._links[chat_id] = link

    async def get(self, chat_id: int) -> str | None:
        async with self._lock:
            return self._links.get(chat_id)

    async def forget(self, chat_id: int) -> None:
        async with self._lock:
            self._links.pop(chat_id, None)


_qr_links = _QrLinkStorage()


class _SingleMessageManager:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._messages: dict[int, int] = {}

    async def send(
        self,
        source: Message,
        sender: Callable[[], Awaitable[Message]],
        *,
        keep_history: bool = False,
    ) -> Message:
        chat_id = source.chat.id
        async with self._lock:
            previous_id = self._messages.get(chat_id)
            if not keep_history and previous_id is not None:
                try:
                    await bot.delete_message(chat_id, previous_id)
                except Exception:
                    logger.debug(
                        "Failed to delete previous bot message",
                        extra={"chat_id": chat_id, "message_id": previous_id},
                    )
            message = await sender()
            if not keep_history:
                self._messages[chat_id] = message.message_id
            return message

    async def remember(self, message: Message) -> None:
        async with self._lock:
            self._messages[message.chat.id] = message.message_id

    async def forget(self, chat_id: int) -> None:
        async with self._lock:
            self._messages.pop(chat_id, None)


_single_messages = _SingleMessageManager()


class _QrCleanupMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data):  # type: ignore[override]
        chat_id: int | None = None

        if isinstance(event, CallbackQuery):
            if event.data == "show_qr":
                return await handler(event, data)
            if event.message:
                chat_id = event.message.chat.id
        elif isinstance(event, Message):
            chat_id = event.chat.id

        if chat_id is None:
            return await handler(event, data)

        try:
            return await handler(event, data)
        finally:
            await _delete_previous_qr(chat_id, forget_link=False)


async def _delete_previous_qr(chat_id: int, *, forget_link: bool = True) -> None:
    if forget_link:
        await _qr_links.forget(chat_id)
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
    region = State()
    preferences = State()


ConversationHistory = Deque[dict[str, str]]
_histories: Dict[int, ConversationHistory] = defaultdict(
    lambda: deque(maxlen=MAX_HISTORY_MESSAGES * 2 if MAX_HISTORY_MESSAGES > 0 else None)
)
BOT_USERNAME: str | None = None


_qr_cleanup_middleware = _QrCleanupMiddleware()
dp.message.middleware.register(_qr_cleanup_middleware)
dp.callback_query.middleware.register(_qr_cleanup_middleware)


MENU_QUICK = "menu_quick"
MENU_AI = "menu_ai"
MENU_KEYS = "menu_keys"
MENU_PAY = "menu_pay"
MENU_REF = "menu_ref"
MENU_HELP = "menu_help"
MENU_BACK = "menu_back"
PAY_PLAN_PREFIX = "pay_plan:"
PAY_CARD_MENU = "pay_card"
_ALLOWED_BUTTON_SCHEMES = {"http", "https", "tg"}
CANCEL_AI = "ai_cancel"


def _build_common_action_rows(include_help: bool = True) -> list[list[InlineKeyboardButton]]:
    action_rows: list[list[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton(text="🚀 Быстрый старт", callback_data=MENU_QUICK),
            InlineKeyboardButton(text="🔑 Мои ключи", callback_data=MENU_KEYS),
        ]
    ]

    payment_row = [InlineKeyboardButton(text="⭐️ Оплатить", callback_data=MENU_PAY)]
    if include_help:
        payment_row.append(InlineKeyboardButton(text="ℹ️ Помощь", callback_data=MENU_HELP))
    action_rows.append(payment_row)

    action_rows.append([InlineKeyboardButton(text="⬅️ Главное меню", callback_data=MENU_BACK)])
    return action_rows


def build_main_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🚀 Быстрый старт", callback_data=MENU_QUICK)],
            [InlineKeyboardButton(text="🧠 Подобрать с ИИ", callback_data=MENU_AI)],
            [InlineKeyboardButton(text="🔑 Мои ключи", callback_data=MENU_KEYS)],
            [InlineKeyboardButton(text="⭐️ Оплатить", callback_data=MENU_PAY)],
            [InlineKeyboardButton(text="🤝 Рефералы", callback_data=MENU_REF)],
            [InlineKeyboardButton(text="ℹ️ Помощь", callback_data=MENU_HELP)],
        ]
    )


def build_back_menu(include_help: bool = True) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=_build_common_action_rows(include_help))


def build_payment_keyboard(username: str, chat_id: int | None, ref: str | None) -> InlineKeyboardMarkup:
    """Show available payment options including Telegram Stars."""

    _ = (username, chat_id, ref)  # preserved for compatibility with callers

    if not STAR_SETTINGS.enabled:
        return build_card_payment_keyboard(username, chat_id, ref)

    rows: list[list[InlineKeyboardButton]] = []
    for plan in _ordered_star_plans(STAR_SETTINGS):
        rows.append(
            [
                InlineKeyboardButton(
                    text=_format_star_plan_button(plan),
                    callback_data=f"{STAR_PAY_PREFIX}{plan.code}",
                )
            ]
        )

    rows.append([InlineKeyboardButton(text="⬅️ Главное меню", callback_data=MENU_BACK)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _build_main_menu_only_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="⬅️ Главное меню", callback_data=MENU_BACK)]]
    )


def _find_active_subscription_key(keys: Sequence[dict[str, Any]]) -> dict[str, Any] | None:
    for key in keys:
        if not key.get("active"):
            continue
        if key.get("trial"):
            continue
        if not key.get("is_subscription"):
            continue
        return key
    return None


def _should_offer_tariffs(keys: Sequence[dict[str, Any]]) -> bool:
    return _find_active_subscription_key(keys) is None


def _format_active_subscription_notice(subscription_key: dict[str, Any]) -> str:
    expires_at = subscription_key.get("expires_at") or "—"
    label = subscription_key.get("label")
    lines = ["🔔 Подписка уже активна."]
    if label:
        lines.append(f"Тариф: {label}")
    lines.append(f"Доступ действует до: {expires_at}.")
    lines.append("Управлять подпиской можно через «🔑 Мои ключи» или настройки Telegram.")
    return _safe_text("\n".join(lines))


def build_card_payment_keyboard(
    username: str, chat_id: int | None, ref: str | None
) -> InlineKeyboardMarkup:
    _ = (username, chat_id, ref)
    rows: list[list[InlineKeyboardButton]] = []
    for plan in PLAN_ORDER:
        price = PLANS[plan]
        label = PLAN_DISPLAY_LABELS.get(plan, plan.upper())
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"{label} · {price}⭐",
                    callback_data=f"{PAY_PLAN_PREFIX}{plan}",
                )
            ]
        )
    rows.append([InlineKeyboardButton(text="⬅️ К выбору оплаты", callback_data=MENU_PAY)])
    rows.append([InlineKeyboardButton(text="⬅️ Главное меню", callback_data=MENU_BACK)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def build_payment_result_keyboard(pay_url: str | None) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if pay_url:
        rows.append([InlineKeyboardButton(text="💳 Оплатить", url=pay_url)])
    rows.append([InlineKeyboardButton(text="⬅️ Выбрать другой тариф", callback_data=PAY_CARD_MENU)])
    rows.append([InlineKeyboardButton(text="⬅️ Главное меню", callback_data=MENU_BACK)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def build_payment_page_url(
    username: str,
    plan: str,
    chat_id: int | None,
    ref: str | None,
    user_id: int | None,
) -> str:
    params: dict[str, str] = {"u": username, "plan": plan, "auto": "1"}
    if chat_id:
        params["c"] = str(chat_id)
    if ref:
        params["r"] = ref
    if user_id:
        params["uid"] = str(user_id)
    if BOT_USERNAME:
        params["bot"] = BOT_USERNAME
    return f"{BOT_PAYMENT_URL}?{urlencode(params)}"


def _safe_text(text: str) -> str:
    sanitized = sanitize_text(text)
    assert_no_geoblocking(sanitized)
    return sanitized


def format_key_info(payload: dict[str, Any], username: str, title: str) -> tuple[str, str | None]:
    lines: list[str] = [title]

    payload_username = payload.get('username')
    if payload_username:
        lines.append(f'Пользователь: {payload_username}')
    else:
        lines.append(f'Пользователь: {username}')

    uuid_value = payload.get('uuid')
    if uuid_value:
        lines.append(f'UUID: {uuid_value}')

    expires = payload.get('expires_at')
    if expires:
        lines.append(f'Действует до: {expires}')

    active = payload.get('active')
    if active is not None:
        status_text = 'активен' if active else 'неактивен'
        lines.append(f'Статус: {status_text}')

    link = payload.get('link')
    if link:
        lines.append('')
        lines.append('🔗 Ссылка для подключения:')
        lines.append(link)

    return _safe_text('\n'.join(lines)), link


def _is_supported_button_link(link: str) -> bool:
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

    if parsed.scheme == "tg":
        return bool(parsed.path or parsed.netloc or parsed.query)

    return False


def build_result_markup(link: str | None = None) -> InlineKeyboardMarkup:
    buttons: list[list[InlineKeyboardButton]] = []
    if link:
        normalized_link = link.strip()
        if normalized_link:
            if _is_supported_button_link(normalized_link):
                buttons.append(
                    [InlineKeyboardButton(text="🔗 Открыть ссылку", url=normalized_link)]
                )
            buttons.append([InlineKeyboardButton(text="Показать QR", callback_data="show_qr")])
    buttons.extend(_build_common_action_rows())
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def _markups_equal(
    first: InlineKeyboardMarkup | None, second: InlineKeyboardMarkup | None
) -> bool:
    if first is second:
        return True
    if first is None or second is None:
        return first is None and second is None
    try:
        return first.model_dump(exclude_none=True) == second.model_dump(exclude_none=True)
    except AttributeError:  # pragma: no cover - fallback for unexpected types
        return first == second


async def edit_message_text_safe(
    message: Message,
    text: str,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> bool:
    if message.text == text and _markups_equal(message.reply_markup, reply_markup):
        return False
    updated = await message.edit_text(text, reply_markup=reply_markup)
    if isinstance(updated, Message):
        await _single_messages.remember(updated)
    else:  # pragma: no cover - defensive branch for unexpected return types
        await _single_messages.remember(message)
    return True


def _get_history(chat_id: int) -> ConversationHistory:
    return _histories[chat_id]


async def send_single_message(
    message: Message,
    text: str,
    *,
    keep_history: bool = False,
    **kwargs: Any,
) -> Message:
    async def _send() -> Message:
        return await message.answer(text, **kwargs)

    return await _single_messages.send(message, _send, keep_history=keep_history)


def _remember_exchange(chat_id: int, user_text: str, reply: str) -> None:
    history = _get_history(chat_id)
    history.append({"role": "user", "content": user_text})
    history.append({"role": "assistant", "content": reply})


def _build_messages(
    chat_id: int, user_text: str, *, extra_system_prompts: Sequence[str] | None = None
) -> list[dict[str, str]]:
    history = list(_get_history(chat_id))
    messages: list[dict[str, str]] = []
    system_prompts = list(SYSTEM_PROMPTS)
    if extra_system_prompts:
        for prompt in extra_system_prompts:
            cleaned = prompt.strip()
            if cleaned:
                system_prompts.append(cleaned)
    for prompt in system_prompts:
        messages.append({"role": "system", "content": prompt})
    messages.extend(history)
    messages.append({"role": "user", "content": user_text})
    return messages


async def ask_gpt(
    chat_id: int, user_text: str, *, extra_system_prompts: Sequence[str] | None = None
) -> str:
    messages = _build_messages(
        chat_id, user_text, extra_system_prompts=extra_system_prompts
    )
    completion = await asyncio.get_event_loop().run_in_executor(
        None,
        lambda: client.chat.completions.create(model=GPT_MODEL, messages=messages),
    )
    reply = completion.choices[0].message.content or ""
    _remember_exchange(chat_id, user_text, reply)
    return reply


DEFAULT_AI_QUESTIONS = [
    "Какое устройство подключаем к VPN?",
    "Где чаще всего будет нужен VPN? Оптимизируем под местных провайдеров.",
    "Есть ли особые пожелания по использованию VPN?",
]


def _format_days(days: int) -> str:
    remainder = abs(days) % 100
    if 11 <= remainder <= 14:
        suffix = "дней"
    else:
        last_digit = abs(days) % 10
        if last_digit == 1:
            suffix = "день"
        elif 2 <= last_digit <= 4:
            suffix = "дня"
        else:
            suffix = "дней"
    return f"{days} {suffix}"


def _build_test_intro() -> str:
    plan = _get_star_plan(TEST_PLAN_CODE)
    if plan:
        return f"тест за {plan.price_stars}⭐ на 24 часа"
    return "тестовый доступ — я подскажу, как его активировать"


def build_ai_questions_prompt() -> str:
    return (
        "Ты помогаешь оператору VPN-сервиса. Сформируй три очень коротких вопроса "
        "для пользователя. Структура строго такая: 1) выясни тип или модель "
        "устройства пользователя; 2) уточни регион основного использования VPN, "
        "упомяни, что оптимизируешь рекомендации под местных провайдеров и особенности GEO; "
        "3) спроси об особых пожеланиях по применению VPN. Каждый вопрос до 90 символов. "
        "Ответ верни в JSON без комментариев и дополнительного текста: "
        '{"questions": ["вопрос1", "вопрос2", "вопрос3"]}. '
        "Используй дружелюбный тон без эмодзи."
    )


def _parse_ai_questions(raw: str) -> list[str]:
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return []

    questions = payload.get("questions")
    if not isinstance(questions, list):
        return []

    parsed: list[str] = []
    for item in questions:
        if isinstance(item, str):
            cleaned = item.strip()
            if cleaned:
                parsed.append(cleaned)
        if len(parsed) == 3:
            break
    return parsed


async def generate_ai_questions(chat_id: int) -> list[str]:
    response = await ask_gpt(chat_id, build_ai_questions_prompt())
    questions = _parse_ai_questions(response)
    if len(questions) == 3:
        return questions
    return DEFAULT_AI_QUESTIONS


def _extract_ai_questions(data: dict[str, Any]) -> list[str]:
    raw_questions = data.get("ai_questions")
    if isinstance(raw_questions, list):
        cleaned = [
            item.strip()
            for item in raw_questions
            if isinstance(item, str) and item.strip()
        ]
        if len(cleaned) >= 3:
            return cleaned[:3]
    return DEFAULT_AI_QUESTIONS


async def _request_with_retry(
    method: str,
    path: str,
    *,
    json_payload: dict[str, Any] | None = None,
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    headers = {"Authorization": f"Bearer {SERVICE_TOKEN}"} if SERVICE_TOKEN else {}
    base_url = VPN_API_URL.rstrip("/")
    delay = API_RETRY_BASE_DELAY

    for attempt in range(API_MAX_RETRIES):
        try:
            async with httpx.AsyncClient(timeout=API_TIMEOUT) as http_client:
                response = await http_client.request(
                    method,
                    f"{base_url}{path}",
                    json=json_payload,
                    params=params,
                    headers=headers,
                )
        except httpx.RequestError as exc:
            logger.warning(
                "Failed to call VPN API",
                extra={
                    "path": path,
                    "method": method,
                    "attempt": attempt + 1,
                    "error": str(exc),
                },
            )
            if attempt + 1 >= API_MAX_RETRIES:
                raise
        else:
            try:
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                status = exc.response.status_code
                if 500 <= status < 600:
                    logger.warning(
                        "VPN API returned server error",
                        extra={
                            "status": status,
                            "path": path,
                            "method": method,
                            "attempt": attempt + 1,
                        },
                    )
                    if attempt + 1 >= API_MAX_RETRIES:
                        raise VpnApiUnavailableError(status) from exc
                else:
                    raise
            else:
                try:
                    return response.json()
                except ValueError:
                    logger.warning(
                        "Failed to decode VPN API response as JSON",
                        extra={"path": path, "method": method},
                    )
                    return {}

        await asyncio.sleep(delay)
        delay *= 2

    raise VpnApiUnavailableError()


async def api_post(path: str, payload: dict[str, Any]) -> dict[str, Any]:
    return await _request_with_retry("POST", path, json_payload=payload)


async def api_get(path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    return await _request_with_retry("GET", path, params=params)


async def create_star_payment_record(
    *,
    user_id: int,
    username: str | None,
    plan: str,
    amount_stars: int,
    charge_id: str | None,
    is_subscription: bool,
    status: str = "paid",
    delivery_pending: bool = False,
) -> dict:
    return await asyncio.to_thread(
        db_utils.create_star_payment,
        user_id=user_id,
        username=username,
        plan=plan,
        amount_stars=amount_stars,
        charge_id=charge_id,
        is_subscription=is_subscription,
        status=status,
        delivery_pending=delivery_pending,
    )


async def get_star_payment_by_charge(charge_id: str) -> dict | None:
    return await asyncio.to_thread(db_utils.get_star_payment_by_charge, charge_id)


async def mark_star_payment_pending(payment_id: int, error: str | None = None) -> dict | None:
    return await asyncio.to_thread(db_utils.mark_star_payment_pending, payment_id, error=error)


async def mark_star_payment_fulfilled(payment_id: int) -> dict | None:
    return await asyncio.to_thread(db_utils.mark_star_payment_fulfilled, payment_id)


async def list_pending_star_payments(username: str) -> list[dict]:
    return await asyncio.to_thread(db_utils.list_pending_star_payments, username)


async def update_star_payment_status(payment_id: int, **fields: Any) -> dict | None:
    return await asyncio.to_thread(db_utils.update_star_payment_status, payment_id, **fields)


async def star_payments_summary(days: int | None = None) -> dict:
    return await asyncio.to_thread(db_utils.star_payments_summary, days)


async def _call_telegram_method(method: str, payload: dict[str, Any]) -> dict[str, Any]:
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.post(url, json=payload)
    except httpx.RequestError as exc:
        logger.exception("Failed to call Telegram method", extra={"method": method, "error": str(exc)})
        raise

    response.raise_for_status()
    data = response.json()
    if not data.get("ok"):
        logger.error("Telegram API returned error", extra={"method": method, "response": data})
        raise RuntimeError(f"telegram_error:{method}")
    return data.get("result")


async def register_user(username: str, chat_id: int, ref: str | None) -> None:
    try:
        await api_post("/users/register", {"username": username, "chat_id": chat_id, "referrer": ref})
    except VpnApiUnavailableError:
        logger.error("VPN API is unavailable while registering user")
    except httpx.HTTPStatusError as exc:
        logger.warning("Failed to register user", extra={"status": exc.response.status_code})


async def apply_referral(referrer: str, referee: str, chat_id: int) -> None:
    try:
        await api_post(
            "/referral/use",
            {"referrer": referrer, "referee": referee, "chat_id": chat_id},
        )
    except VpnApiUnavailableError:
        logger.info("Referral service temporarily unavailable")
    except httpx.HTTPStatusError as exc:
        logger.info("Referral not applied", extra={"status": exc.response.status_code})


async def fetch_keys(username: str) -> list[dict[str, Any]]:
    try:
        response = await api_get(f"/users/{username}/keys")
        if response.get("ok"):
            return response.get("keys", [])
    except VpnApiUnavailableError as exc:
        logger.error(
            "VPN API is unavailable when fetching keys",
            extra={"status": exc.status_code},
        )
    except httpx.HTTPStatusError as exc:
        logger.exception("Failed to fetch keys", extra={"status": exc.response.status_code})
    return []


async def fetch_referral_stats(username: str) -> dict[str, Any]:
    try:
        response = await api_get(f"/users/{username}/referrals")
        if response.get("ok"):
            return response
    except VpnApiUnavailableError as exc:
        logger.error(
            "VPN API is unavailable when fetching referral stats",
            extra={"status": exc.status_code},
        )
    except httpx.HTTPStatusError:
        pass
    return {"username": username, "total_referrals": 0, "total_days": 0}


async def renew_star_plan(username: str, plan_code: str, chat_id: int | None) -> dict[str, Any]:
    payload: dict[str, Any] = {"username": username}
    if chat_id is not None:
        payload["chat_id"] = chat_id

    plan = _get_star_plan(plan_code)
    try:
        duration_days = resolve_plan_duration(plan_code)
    except RuntimeError:
        duration_days = None

    if plan_code in {"1m", "3m", "1y", "12m"}:
        payload["plan"] = plan_code
    elif duration_days is not None:
        payload["days"] = duration_days
    else:
        raise RuntimeError(f"unknown_plan:{plan_code}")

    if plan is not None:
        payload["is_subscription"] = plan.is_subscription

    response = await api_post("/vpn/renew_key", payload)
    if not response.get("ok"):
        detail = response.get("detail") or response.get("error") or "renew_failed"
        raise RuntimeError(f"renew_failed:{detail}")
    return response


async def revoke_access_after_refund(username: str, plan_code: str) -> None:
    try:
        duration_days = resolve_plan_duration(plan_code)
    except RuntimeError:
        duration_days = 0
    if duration_days <= 0:
        duration_days = 30
    try:
        await api_post("/vpn/renew_key", {"username": username, "days": -abs(duration_days)})
    except Exception as exc:  # pragma: no cover - defensive logging
        logger.exception(
            "Failed to revoke VPN access after refund",
            extra={"username": username, "plan": plan_code, "error": str(exc)},
        )


async def _delete_previous_qr_for_stars(chat_id: int) -> None:
    await _delete_previous_qr(chat_id)


setup_stars_handlers(
    dp,
    StarHandlerDependencies(
        settings=STAR_SETTINGS,
        pay_prefix=STAR_PAY_PREFIX,
        build_result_markup=build_result_markup,
        remember_qr=_qr_links.remember,
        delete_previous_qr=_delete_previous_qr_for_stars,
        format_key_info=format_key_info,
        register_user=register_user,
        renew_access=renew_star_plan,
        create_payment_record=create_star_payment_record,
        get_payment_by_charge=get_star_payment_by_charge,
        mark_payment_pending=mark_star_payment_pending,
        mark_payment_fulfilled=mark_star_payment_fulfilled,
        list_pending_payments=list_pending_star_payments,
        send_single_message=send_single_message,
        logger=logger,
    ),
)


def format_key_message(payload: dict[str, Any]) -> str:
    expires = payload.get("expires_at", "—")
    parts = ["<b>VPN-ключ</b>"]

    if expires:
        parts.extend(["", f"Действует до: {expires}"])

    link = payload.get("link")
    if link:
        parts.extend(["", f"<code>{link}</code>"])

    return "\n".join(parts)


def build_ai_instruction_prompt(
    device: str, region: str, preferences: str, stars: StarSettings
) -> str:
    test_plan = _get_star_plan(TEST_PLAN_CODE)
    month_plan = stars.plans.get("1m")
    extra_codes = [code for code in ("3m", "1y", "12m") if code in stars.plans]
    extras = [stars.plans[code] for code in extra_codes if code in stars.plans]

    test_phrase = (
        f"Тест: {test_plan.price_stars}⭐ за 24 часа"
        if test_plan
        else "Тест активируется через Telegram Stars"
    )
    month_phrase = (
        f"1 месяц — {month_plan.price_stars}⭐"
        if month_plan
        else "Месячный тариф доступен в боте"
    )
    extra_phrase = (
        "; ".join(f"{plan.title} — {plan.price_stars}⭐" for plan in extras)
        if extras
        else ""
    )
    tariff_info = ", ".join(filter(None, [test_phrase, month_phrase, extra_phrase]))

    return (
        "Ты помогаешь пользователю настроить VPN. Сформируй лаконичную памятку из трёх пунктов: "
        "1) выбери приложение под устройство, 2) опиши импорт VLESS-ссылки, 3) расскажи, как оплатить доступ звёздами. "
        "Пиши дружелюбно и понятно, избегай жаргона и длинных вступлений.\n"
        f"Устройство: {device}.\nРегион использования: {region}.\nОсобые пожелания: {preferences}.\n"
        f"Тарифы: {tariff_info}.\n"
        "Отвечай в формате списка с короткими предложениями."
    )


def build_ai_keyboard(link: str | None, username: str, chat_id: int, ref: str | None) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if link:
        normalized_link = link.strip()
        if normalized_link:
            if _is_supported_button_link(normalized_link):
                rows.append([InlineKeyboardButton(text="📥 Импортировать", url=normalized_link)])
            rows.append([InlineKeyboardButton(text="Показать QR", callback_data="show_qr")])
    rows.append([InlineKeyboardButton(text="⭐️ Оплатить", callback_data=MENU_PAY)])
    rows.append([InlineKeyboardButton(text="⬅️ Главное меню", callback_data=MENU_BACK)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def build_help_text() -> str:
    recommendations = _format_vless_clients_recommendations("   ")
    return (
        "ℹ️ <b>Нужна помощь?</b>\n"
        "1. Выбери и установи приложение из списка рекомендаций ниже:\n"
        f"{recommendations}\n"
        "2. Импортируй ссылку VLESS из карточки ключа.\n"
        "3. Если что-то не получается – пиши в чат прямо здесь и сейчас. Я всегда на связи 😉"
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
    await ensure_star_deliveries(message, username)
    await bot.set_chat_menu_button(message.chat.id, MenuButtonDefault())

    test_intro = _build_test_intro()
    greeting = (
        "👋 Привет! Я VPN_GPT — помогу подключиться к VPN в три шага:\n"
        f"1️⃣ Активируй {test_intro}.\n"
        "2️⃣ Следуй инструкции и подключи приложение.\n"
        "3️⃣ Выбери тариф и пользуйся без ограничений."
    )
    await send_single_message(message, greeting, reply_markup=build_main_menu())


@dp.callback_query(F.data == MENU_BACK)
async def handle_menu_back(call: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    message = call.message
    if not message:
        await call.answer()
        return
    await _delete_previous_qr(message.chat.id)
    await edit_message_text_safe(message, "Выбери действие:", reply_markup=build_main_menu())
    await call.answer()


@dp.callback_query(F.data == MENU_QUICK)
async def handle_quick_start(call: CallbackQuery) -> None:
    user = call.from_user
    if user is None:
        await call.answer()
        return
    message = call.message
    if not message:
        await call.answer()
        return
    await _delete_previous_qr(message.chat.id)
    username = user.username or f"id_{user.id}"
    await register_user(username, message.chat.id, None)
    await ensure_star_deliveries(message, username)

    keys = await fetch_keys(username)
    active_keys = [key for key in keys if key.get("active")]
    if active_keys:
        text = _format_active_key_quick_start_message(active_keys)
        subscription_key = _find_active_subscription_key(active_keys)
        if subscription_key:
            reply_markup = _build_main_menu_only_keyboard()
        else:
            reply_markup = build_payment_keyboard(username, message.chat.id, username)
        await edit_message_text_safe(message, text, reply_markup=reply_markup)
        await call.answer("Доступ уже активен")
        return

    test_plan = _get_star_plan(TEST_PLAN_CODE)

    if not STAR_SETTINGS.enabled or test_plan is None:
        await edit_message_text_safe(
            message,
            "Сейчас тестовый доступ выдаётся после оплаты звёздами. Напиши в чат, и я помогу оформить покупку вручную.",
            reply_markup=build_back_menu(),
        )
        await call.answer()
        return

    lines = [
        "🎯 <b>Тестовый доступ на 24 часа</b>",
        f"Стоимость — {test_plan.price_stars}⭐. Оплата проходит прямо в Telegram, после неё ключ приходит мгновенно.",
        "После теста сможешь выбрать удобный тариф.",
        "",
        "Что делать после оплаты:",
        "• Ключ доступа придёт сразу после оплаты (просто, нажми на него, чтобы скопировать).",
        "• Установи приложение (ссылка ниже).",
        "• В приложении добавь ключ через \"Импорт из буфера\"",
        "• Рекомендуем, так же, в настройках приложения выбрать \"Установить системные прокси\"",
        "",
        "📱 <b>Рекомендуемые приложения:</b>",
        _format_vless_clients_recommendations(),
    ]

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=f"⭐️ {test_plan.title} · {test_plan.price_stars}⭐",
                    callback_data=f"{STAR_PAY_PREFIX}{test_plan.code}",
                )
            ],
            [InlineKeyboardButton(text="⬅️ Главное меню", callback_data=MENU_BACK)],
        ]
    )

    await edit_message_text_safe(message, "\n".join(lines), reply_markup=keyboard)
    await call.answer("Готово")


@dp.callback_query(F.data == "show_qr")
async def handle_show_qr(callback: CallbackQuery) -> None:
    if not callback.message:
        await callback.answer()
        return

    chat_id = callback.message.chat.id
    link = await _qr_links.get(chat_id)
    if not link:
        await callback.answer("QR недоступен", show_alert=True)
        return

    await _delete_previous_qr(chat_id)

    qr = make_qr(link)
    qr_message = await callback.message.answer_photo(
        BufferedInputFile(qr.getvalue(), filename="vpn_key.png"),
        caption="📱 Отсканируй, чтобы добавить ключ в приложение",
    )
    normalized_link = link.strip()
    if normalized_link:
        await _qr_links.remember(chat_id, normalized_link)
    await _qr_messages.remember(chat_id, qr_message.message_id)
    await callback.answer()


@dp.callback_query(F.data == MENU_KEYS)
async def handle_my_keys(call: CallbackQuery) -> None:
    user = call.from_user
    if user is None:
        await call.answer()
        return
    message = call.message
    if not message:
        await call.answer()
        return
    await _delete_previous_qr(message.chat.id)
    username = user.username or f"id_{user.id}"
    await ensure_star_deliveries(message, username)
    keys = await fetch_keys(username)
    active_keys = [key for key in keys if key.get("active")]
    if not active_keys:
        test_plan = _get_star_plan(TEST_PLAN_CODE)
        if test_plan:
            text = (
                f"Пока активных ключей нет. Нажми «Быстрый старт», чтобы взять тест за {test_plan.price_stars}⭐ и проверить сервис!"
            )
        else:
            text = "Пока активных ключей нет. Нажми «Быстрый старт», я помогу подключиться."
    else:
        parts = ["🔑 <b>Твои ключи</b>"]
        for idx, key in enumerate(active_keys, start=1):
            parts.append(
                f"\n<b>#{idx}</b> · ✅ активен\nДействует до: {key.get('expires_at', '—')}"
            )
            if key.get("is_subscription"):
                parts.append("Формат: подписка с автопродлением.")
            else:
                parts.append("Формат: разовая активация.")
            if key.get("link"):
                parts.append(f"<code>{key['link']}</code>")
        text = "\n".join(parts)
    if _should_offer_tariffs(active_keys):
        reply_markup = build_payment_keyboard(username, message.chat.id, username)
    else:
        reply_markup = _build_main_menu_only_keyboard()
    await edit_message_text_safe(message, text, reply_markup=reply_markup)
    await call.answer()


@dp.callback_query(F.data == MENU_PAY)
async def handle_pay(call: CallbackQuery) -> None:
    user = call.from_user
    if user is None:
        await call.answer()
        return
    message = call.message
    if not message:
        await call.answer()
        return
    await _delete_previous_qr(message.chat.id)
    username = user.username or f"id_{user.id}"
    await ensure_star_deliveries(message, username)
    keys = await fetch_keys(username)
    active_keys = [key for key in keys if key.get("active")]
    subscription_key = _find_active_subscription_key(active_keys)
    if subscription_key:
        notice = _format_active_subscription_notice(subscription_key)
        await edit_message_text_safe(
            message,
            notice,
            reply_markup=_build_main_menu_only_keyboard(),
        )
        await call.answer()
        return

    if STAR_SETTINGS.enabled:
        test_plan = _get_star_plan(TEST_PLAN_CODE)
        if test_plan:
            text = (
                f"Выбери тариф и оплати звёздами прямо в Telegram. Тест на 24 часа стоит {test_plan.price_stars}⭐."
            )
        else:
            text = "Выбери тариф и оплати звёздами прямо в Telegram."
    else:
        text = "Оплата временно недоступна. Напиши в чат, и я помогу оформить доступ вручную."
    text += (
        "\n\nℹ️ Уже действующие клиенты пользуются своими ключами до конца обещанного тестового периода."
        " После завершения доступа нужно будет выбрать один из новых тарифов."
    )
    keyboard = build_payment_keyboard(username, message.chat.id, username)
    await edit_message_text_safe(message, text, reply_markup=keyboard)
    await call.answer()


@dp.callback_query(F.data == PAY_CARD_MENU)
async def handle_card_menu(call: CallbackQuery) -> None:
    user = call.from_user
    if user is None:
        await call.answer()
        return
    message = call.message
    if not message:
        await call.answer()
        return
    await _delete_previous_qr(message.chat.id)
    username = user.username or f"id_{user.id}"
    await ensure_star_deliveries(message, username)
    await edit_message_text_safe(
        message,
        "Оплата картой временно отключена. Используй оплату звёздами — она доступна в главном меню.",
        reply_markup=build_back_menu(),
    )
    await call.answer()


@dp.callback_query(F.data.startswith(PAY_PLAN_PREFIX))
async def handle_pay_plan(call: CallbackQuery) -> None:
    user = call.from_user
    if user is None:
        await call.answer("Не удалось определить пользователя", show_alert=True)
        return

    plan = call.data[len(PAY_PLAN_PREFIX) :]
    if plan not in PLANS:
        await call.answer("Неизвестный тариф", show_alert=True)
        return

    message = call.message
    chat_id = message.chat.id if message else None
    username = user.username or f"id_{user.id}"
    if message:
        await _delete_previous_qr(message.chat.id)
        await ensure_star_deliveries(message, username)

    await register_user(username, chat_id, user.username)

    payment_url = build_payment_page_url(username, plan, chat_id, user.username, user.id)
    amount = PLANS.get(plan)
    if amount is not None:
        price_text = f"Сумма к оплате: {amount}⭐."
    else:
        price_text = ""

    logger.info(
        "Redirecting user to payment page",
        extra={"plan": plan, "username": username, "chat_id": chat_id},
    )
    plan_label = PLAN_DISPLAY_LABELS.get(plan, plan.upper())
    text_parts = [
        f"💳 Тариф {plan_label} готов к оплате.",
        "Нажми «Оплатить», мы передадим данные на сайт и сформируем ссылку оплаты.",
    ]
    if price_text:
        text_parts.insert(1, price_text)
    text = "\n".join(text_parts)
    markup = build_payment_result_keyboard(payment_url)

    if message:
        await edit_message_text_safe(message, text, reply_markup=markup)
    await call.answer()


@dp.callback_query(F.data == MENU_REF)
async def handle_referrals(call: CallbackQuery) -> None:
    user = call.from_user
    if user is None:
        await call.answer()
        return
    message = call.message
    if not message:
        await call.answer()
        return
    await _delete_previous_qr(message.chat.id)
    username = user.username or f"id_{user.id}"
    stats = await fetch_referral_stats(username)
    ref_link = f"https://t.me/{BOT_USERNAME}?start={username}" if BOT_USERNAME else ""
    text = (
        "🤝 <b>Реферальная программа</b>\n"
        f"Пригласи друга — и после его оплаты получи +{REFERRAL_BONUS_DAYS} дней.\n\n"
        f"Твой прогресс: {stats.get('total_referrals', 0)} приглашений, {stats.get('total_days', 0)} бонусных дней.\n"
        f"Ссылка: {ref_link or 'поделись своим @username'}"
    )
    await edit_message_text_safe(message, text, reply_markup=build_back_menu())
    await call.answer()


@dp.callback_query(F.data == MENU_HELP)
async def handle_help(call: CallbackQuery) -> None:
    message = call.message
    if not message:
        await call.answer()
        return
    await _delete_previous_qr(message.chat.id)
    await edit_message_text_safe(
        message, build_help_text(), reply_markup=build_back_menu(include_help=False)
    )
    await call.answer()


@dp.callback_query(F.data == MENU_AI)
async def handle_ai_start(call: CallbackQuery, state: FSMContext) -> None:
    message = call.message
    if message is None:
        await call.answer()
        return
    await _delete_previous_qr(message.chat.id)

    chat_id = message.chat.id
    _get_history(chat_id).clear()
    questions = await generate_ai_questions(chat_id)
    _get_history(chat_id).clear()

    await state.set_state(AiFlow.device)
    await state.update_data(ai_questions=questions)
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data=CANCEL_AI)]]
    )
    first_question = questions[0] if questions else DEFAULT_AI_QUESTIONS[0]
    intro_text = "🧠 Давай подберём оптимальный сценарий.\n\n" + first_question
    await edit_message_text_safe(message, intro_text, reply_markup=keyboard)
    await call.answer()


@dp.callback_query(F.data == CANCEL_AI)
async def handle_ai_cancel(call: CallbackQuery, state: FSMContext) -> None:
    message = call.message
    if not message:
        await call.answer()
        return
    await _delete_previous_qr(message.chat.id)
    await state.clear()
    await edit_message_text_safe(
        message, "Ок! Возвращаемся в меню.", reply_markup=build_main_menu()
    )
    await call.answer()


@dp.message(AiFlow.device)
async def process_ai_device(message: Message, state: FSMContext) -> None:
    await _delete_previous_qr(message.chat.id)
    user_device = message.text.strip()
    await state.update_data(device=user_device)
    data = await state.get_data()
    questions = _extract_ai_questions(data)
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data=CANCEL_AI)]]
    )
    next_question = questions[1]
    await send_single_message(message, next_question, reply_markup=keyboard)
    await state.set_state(AiFlow.region)


@dp.message(AiFlow.region)
async def process_ai_region(message: Message, state: FSMContext) -> None:
    await _delete_previous_qr(message.chat.id)
    region = message.text.strip()
    await state.update_data(region=region)
    data = await state.get_data()
    questions = _extract_ai_questions(data)
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data=CANCEL_AI)]]
    )
    next_question = questions[2]
    await send_single_message(message, next_question, reply_markup=keyboard)
    await state.set_state(AiFlow.preferences)


@dp.message(AiFlow.preferences)
async def process_ai_preferences(message: Message, state: FSMContext) -> None:
    await _delete_previous_qr(message.chat.id)
    data = await state.get_data()
    device = data.get("device", "устройство не указано")
    region = data.get("region", "регион не указан")
    preferences = message.text.strip() or "не указаны"
    await state.clear()

    user = message.from_user
    if user is None:
        return
    username = user.username or f"id_{user.id}"
    await register_user(username, message.chat.id, None)

    prompt = build_ai_instruction_prompt(device, region, preferences, STAR_SETTINGS)
    ai_message = await ask_gpt(
        message.chat.id,
        prompt,
        extra_system_prompts=[_VLESS_CLIENTS_SYSTEM_PROMPT],
    )

    response_parts = ["🧠 <b>Твой персональный план</b>", ai_message.strip()]
    test_plan = _get_star_plan(TEST_PLAN_CODE)
    month_plan = _get_star_plan("1m")
    if test_plan:
        info = [
            "\n⭐️ <b>Как протестировать сервис</b>",
            f"Оформи тест на 24 часа за {test_plan.price_stars}⭐ — кнопка ниже откроет оплату прямо в Telegram.",
        ]
        if month_plan:
            info.append(
                f"Когда понравится, переходи на месяц за {month_plan.price_stars}⭐ или выбирай другие тарифы."
            )
        response_parts.extend(info)
    else:
        response_parts.append(
            "\n⭐️ Тестовый доступ оформляется через раздел оплаты. Нажми «Оплатить», и я всё подскажу."
        )

    keyboard = build_ai_keyboard(None, username, message.chat.id, user.username)
    await send_single_message(
        message,
        "\n".join(response_parts),
        reply_markup=keyboard,
    )


@dp.message(Command("help"))
async def command_help(message: Message):
    await _delete_previous_qr(message.chat.id)
    await send_single_message(
        message,
        build_help_text(),
        reply_markup=build_back_menu(),
    )


def _parse_command_arguments(text: str) -> list[str]:
    if not text:
        return []
    parts = text.strip().split()
    return parts[1:]


@dp.message(Command("stars_tx"))
async def command_stars_transactions(message: Message):
    await _delete_previous_qr(message.chat.id)
    user = message.from_user
    if not _is_admin_user(message, from_user=user):
        await send_single_message(
            message,
            "Команда доступна только администраторам.",
        )
        return

    args = _parse_command_arguments(message.text or "")
    try:
        limit = max(1, min(50, int(args[0]))) if args else 10
    except ValueError:
        limit = 10

    try:
        result = await _call_telegram_method("getStarTransactions", {"limit": limit})
    except Exception as exc:
        logger.exception("Failed to fetch star transactions", extra={"error": str(exc)})
        await send_single_message(
            message,
            "Не удалось получить транзакции из Telegram. Проверьте логи.",
        )
        return

    transactions = result.get("transactions") if isinstance(result, dict) else result
    if not transactions:
        await send_single_message(message, "Транзакции не найдены.")
        return

    lines: list[str] = []
    for tx in transactions[:limit]:
        tx_id = tx.get("id") if isinstance(tx, dict) else None
        amount = None
        if isinstance(tx, dict):
            amount_info = tx.get("amount") or tx.get("star_amount") or tx.get("total_amount")
            if isinstance(amount_info, dict):
                amount_value = amount_info.get("amount") or amount_info.get("total_amount")
                amount_currency = amount_info.get("currency") or "XTR"
                amount = f"{amount_value} {amount_currency}" if amount_value is not None else None
            elif amount_info is not None:
                amount = f"{amount_info} XTR"
        amount = amount or "—"
        status = tx.get("status") if isinstance(tx, dict) else "—"
        created = tx.get("date") or tx.get("created_at") if isinstance(tx, dict) else "—"
        purpose = tx.get("type") or tx.get("purpose") if isinstance(tx, dict) else "—"
        lines.append(f"#{tx_id} · {amount} · {purpose} · {status} · {created}")

    response = "Последние транзакции:\n" + "\n".join(lines)
    await send_single_message(message, response)


@dp.message(Command("stars_refund"))
async def command_stars_refund(message: Message):
    await _delete_previous_qr(message.chat.id)
    user = message.from_user
    if not _is_admin_user(message, from_user=user):
        await send_single_message(
            message,
            "Команда доступна только администраторам.",
        )
        return

    args = _parse_command_arguments(message.text or "")
    if not args:
        await send_single_message(
            message,
            "Использование: /stars_refund <charge_id>",
        )
        return

    charge_id = args[0].strip()
    if not charge_id:
        await send_single_message(
            message,
            "Укажите идентификатор платежа (charge_id).",
        )
        return

    try:
        await _call_telegram_method("refundStarPayment", {"charge_id": charge_id})
    except Exception as exc:
        logger.exception("Failed to refund Stars payment", extra={"charge_id": charge_id, "error": str(exc)})
        await send_single_message(
            message,
            "Не удалось выполнить рефанд через Telegram. Проверьте логи.",
        )
        return

    record = await get_star_payment_by_charge(charge_id)
    if record and record.get("id"):
        await update_star_payment_status(
            int(record["id"]),
            status="refunded",
            refunded_at=datetime.now(UTC),
        )
        username = record.get("username")
        plan_code = record.get("plan") or "1m"
        if username:
            await revoke_access_after_refund(username, plan_code)

    await send_single_message(
        message,
        "Рефанд Stars инициирован. Статус: успешно.",
    )


@dp.message(Command("stars_stats"))
async def command_stars_stats(message: Message):
    await _delete_previous_qr(message.chat.id)
    user = message.from_user
    if not _is_admin_user(message, from_user=user):
        await send_single_message(
            message,
            "Команда доступна только администраторам.",
        )
        return

    args = _parse_command_arguments(message.text or "")
    days: int | None = None
    if args:
        try:
            parsed = int(args[0])
            if parsed > 0:
                days = parsed
        except ValueError:
            pass

    summary = await star_payments_summary(days)
    paid = summary.get("paid", {})
    refunded = summary.get("refunded", {})
    canceled = summary.get("canceled", {})
    failed = summary.get("failed", {})

    total_paid = paid.get("total", 0)
    total_count = paid.get("count", 0)
    refunded_total = refunded.get("total", 0)
    refunded_count = refunded.get("count", 0)
    canceled_count = canceled.get("count", 0)
    failed_count = failed.get("count", 0)

    period_text = f"за последние {days} дн." if days else "за всё время"
    lines = [
        f"Статистика Stars ({period_text}):",
        f"• Оплачено: {total_count} транзакций на {total_paid}⭐",
        f"• Рефанды: {refunded_count} на {refunded_total}⭐",
        f"• Отменено: {canceled_count}",
        f"• Ошибки выдачи: {failed_count}",
    ]
    await send_single_message(message, "\n".join(lines))


@dp.message()
async def handle_message(message: Message) -> None:
    await _delete_previous_qr(message.chat.id)
    user = message.from_user
    if user is None or not message.text:
        return
    username = user.username or f"id_{user.id}"
    await ensure_star_deliveries(message, username)
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
