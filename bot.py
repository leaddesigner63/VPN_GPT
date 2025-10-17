from __future__ import annotations

import asyncio
import json
import logging
import os
from collections import defaultdict, deque
from pathlib import Path
from typing import Any, Deque, Dict
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
TRIAL_DAYS = _get_int_env("TRIAL_DAYS", 0)
PLAN_ENV = os.getenv("PLANS", "1m:180,3m:460,12m:1450")
REFERRAL_BONUS_DAYS = _get_int_env("REFERRAL_BONUS_DAYS", 30)
API_TIMEOUT = _get_float_env("VPN_API_TIMEOUT", 15.0)
API_MAX_RETRIES = max(1, _get_int_env("VPN_API_MAX_RETRIES", 3))
API_RETRY_BASE_DELAY = _get_float_env("VPN_API_RETRY_BASE_DELAY", 0.5)

_VLESS_CLIENTS_RECOMMENDATIONS_PATH = Path(__file__).resolve().parent / "VLESS_clients_recommendations_ru.txt"
_DEFAULT_VLESS_CLIENTS_RECOMMENDATIONS = (
    "• Android — v2rayNG (Google Play): https://play.google.com/store/apps/details?id=com.v2ray.ang\n"
    "• iOS — Stash (App Store): https://apps.apple.com/app/stash-rule-based-proxy/id1596063349\n"
    "• Windows — v2rayN (Microsoft Store): https://apps.microsoft.com/store/detail/v2rayn/9NKBQF3F8K6H\n"
    "• macOS — Stash (Mac App Store): https://apps.apple.com/app/stash-rule-based-proxy/id1596063349\n"
    "• Linux — v2rayA: https://github.com/v2rayA/v2rayA"
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
_ALLOWED_BUTTON_SCHEMES = {"http", "https", "tg"}
CANCEL_AI = "ai_cancel"


def _build_common_action_rows(include_help: bool = True) -> list[list[InlineKeyboardButton]]:
    action_rows: list[list[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton(text="🚀 Быстрый старт", callback_data=MENU_QUICK),
            InlineKeyboardButton(text="🔑 Мои ключи", callback_data=MENU_KEYS),
        ]
    ]

    payment_row = [InlineKeyboardButton(text="💳 Оплатить", callback_data=MENU_PAY)]
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
            [InlineKeyboardButton(text="💳 Оплатить", callback_data=MENU_PAY)],
            [InlineKeyboardButton(text="🤝 Рефералы", callback_data=MENU_REF)],
            [InlineKeyboardButton(text="ℹ️ Помощь", callback_data=MENU_HELP)],
        ]
    )


def build_back_menu(include_help: bool = True) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=_build_common_action_rows(include_help))


def build_payment_keyboard(username: str, chat_id: int | None, ref: str | None) -> InlineKeyboardMarkup:
    """Show tariffs with callbacks that trigger invoice creation."""

    _ = (username, chat_id, ref)  # preserved for compatibility with callers
    rows: list[list[InlineKeyboardButton]] = []
    for plan in PLAN_ORDER:
        price = PLANS[plan]
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"{plan.upper()} · {price} ₽",
                    callback_data=f"{PAY_PLAN_PREFIX}{plan}",
                )
            ]
        )
    rows.append([InlineKeyboardButton(text="⬅️ Главное меню", callback_data=MENU_BACK)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def build_payment_result_keyboard(pay_url: str | None) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if pay_url:
        rows.append([InlineKeyboardButton(text="💳 Оплатить", url=pay_url)])
    rows.append([InlineKeyboardButton(text="⬅️ Выбрать другой тариф", callback_data=MENU_PAY)])
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
    await message.edit_text(text, reply_markup=reply_markup)
    return True


def _get_history(chat_id: int) -> ConversationHistory:
    return _histories[chat_id]


def _remember_exchange(chat_id: int, user_text: str, reply: str) -> None:
    history = _get_history(chat_id)
    history.append({"role": "user", "content": user_text})
    history.append({"role": "assistant", "content": reply})


def _build_messages(chat_id: int, user_text: str) -> list[dict[str, str]]:
    history = list(_get_history(chat_id))
    messages: list[dict[str, str]] = []
    for prompt in SYSTEM_PROMPTS:
        messages.append({"role": "system", "content": prompt})
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


def _build_trial_phrase(days: int) -> str:
    if days > 0:
        return f"тест на {_format_days(days)}"
    return "тест бесплатно"


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


async def issue_trial_key(username: str, chat_id: int) -> dict[str, Any] | None:
    try:
        payload = await api_post(
            "/vpn/issue_key",
            {"username": username, "chat_id": chat_id, "trial": True},
        )
        if not payload.get("ok"):
            return None
        return payload
    except VpnApiUnavailableError:
        logger.error("VPN API is unavailable when issuing key")
        return {"ok": False, "error": "service_unavailable"}
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


def build_ai_instruction_prompt(
    device: str, region: str, preferences: str, trial_days: int, plans: Dict[str, int]
) -> str:
    plan_parts = [f"{code.upper()} — {price} ₽" for code, price in plans.items()]
    return (
        "Ты помогаешь пользователю настроить VPN. Сформируй короткую памятку из 3-4 пунктов: "
        "1) какую программу установить под устройство, 2) как импортировать ссылку VLESS, 3) как оплатить тариф. "
        "Пиши дружелюбно, без жаргона, используй эмодзи экономно.\n"
        f"Устройство: {device}.\nРегион использования: {region}.\nОсобые пожелания: {preferences}.\n"
        f"Триал: {trial_days} дней. Тарифы: {', '.join(plan_parts)}.\n"
        "Опирайся на список рекомендованных приложений ниже, выбирай подходящее под устройство пользователя.\n"
        f"Рекомендации:\n{_VLESS_CLIENTS_RECOMMENDATIONS}"
    )


def build_ai_keyboard(link: str | None, username: str, chat_id: int, ref: str | None) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if link:
        normalized_link = link.strip()
        if normalized_link:
            if _is_supported_button_link(normalized_link):
                rows.append([InlineKeyboardButton(text="📥 Импортировать", url=normalized_link)])
            rows.append([InlineKeyboardButton(text="Показать QR", callback_data="show_qr")])
    rows.append([InlineKeyboardButton(text="💳 Оплатить", callback_data=MENU_PAY)])
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
    await bot.set_chat_menu_button(message.chat.id, MenuButtonDefault())

    trial_phrase = _build_trial_phrase(TRIAL_DAYS)
    greeting = (
        "👋 Привет! Я VPN_GPT — помогу подключиться к VPN в три шага:\n"
        f"1️⃣ Получи ключ ({trial_phrase}).\n"
        "2️⃣ Следуй инструкции, подключи приложение.\n"
        "3️⃣ Оплати подходящий тариф — и пользуйся без ограничений."
    )
    await message.answer(greeting, reply_markup=build_main_menu())


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
    payload = await issue_trial_key(username, message.chat.id)
    if not payload:
        await edit_message_text_safe(
            message,
            "⚠️ Не удалось выдать ключ. Попробуй позже или свяжись с поддержкой.",
            reply_markup=build_back_menu(),
        )
        await call.answer()
        return

    if payload.get("error") == "service_unavailable":
        await edit_message_text_safe(
            message,
            "😔 Сейчас не удаётся выдать ключи — сервис недоступен. "
            "Мы уже работаем над решением. Попробуй позже или напиши в поддержку.",
            reply_markup=build_back_menu(),
        )
        await call.answer()
        return

    if payload.get("error") == "trial_already_used":
        await edit_message_text_safe(
            message,
            "У тебя уже есть активный тестовый ключ. Посмотри его в разделе «Мои ключи».",
            reply_markup=build_back_menu(),
        )
        await call.answer()
        return

    link = payload.get("link")
    text = (
        "🎁 Готово! Твой тестовый доступ активирован."\
        + "\n\n"
        + format_key_message(payload)
        + "\n\n"
        + "ℹ️ Что делать дальше:\n"
        + "1️⃣ Скопируй ссылку выше или открой QR-код.\n"
        + "2️⃣ Вставь её в приложение для VLESS (например, v2rayNG, Stash и т.п.).\n"
        + "3️⃣ Сохрани профиль и включи VPN.\n\n"
        + "📱 <b>Рекомендуемые приложения:</b>\n"
        + _format_vless_clients_recommendations()
    )
    await edit_message_text_safe(message, text, reply_markup=build_result_markup(link))
    if link:
        normalized_link = link.strip()
        if normalized_link:
            await _qr_links.remember(message.chat.id, normalized_link)
    await call.answer("Ключ выдан")


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
    reply_markup = build_payment_keyboard(username, message.chat.id, username)
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
    text = (
        "Выбери тариф. Мы создадим счёт автоматически и отправим безопасную ссылку на оплату."
    )
    keyboard = build_payment_keyboard(username, message.chat.id, username)
    await edit_message_text_safe(message, text, reply_markup=keyboard)
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

    await register_user(username, chat_id, user.username)

    payment_url = build_payment_page_url(username, plan, chat_id, user.username, user.id)
    amount = PLANS.get(plan)
    if amount is not None:
        price_text = f"Сумма к оплате: {amount} ₽."
    else:
        price_text = ""

    logger.info(
        "Redirecting user to payment page",
        extra={"plan": plan, "username": username, "chat_id": chat_id},
    )
    text_parts = [
        f"💳 Тариф {plan.upper()} готов к оплате.",
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
    await message.answer(next_question, reply_markup=keyboard)
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
    await message.answer(next_question, reply_markup=keyboard)
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

    trial_payload = await issue_trial_key(username, message.chat.id)
    if trial_payload and trial_payload.get("error") == "trial_already_used":
        trial_payload = None

    link = trial_payload.get("link") if trial_payload else None

    prompt = build_ai_instruction_prompt(device, region, preferences, TRIAL_DAYS, PLANS)
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
        normalized_link = link.strip()
        if normalized_link:
            await _qr_links.remember(message.chat.id, normalized_link)


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
