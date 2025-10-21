import asyncio
import logging
import os
from typing import Any, Awaitable, Callable
from urllib.parse import urlparse

import httpx
from aiogram import BaseMiddleware, Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    MenuButtonDefault,
    Message,
)

from config import BOT_TOKEN
from utils.content_filters import assert_no_geoblocking, sanitize_text
from utils.qrgen import make_qr

logger = logging.getLogger("vpn_gpt.simple_bot")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()


def _safe_text(text: str) -> str:
    sanitized = sanitize_text(text)
    assert_no_geoblocking(sanitized)
    return sanitized


def _get_trial_days() -> int:
    raw = os.getenv("TRIAL_DAYS")
    if raw is None:
        return 0

    cleaned = raw.strip()
    if not cleaned:
        return 0

    try:
        return int(cleaned)
    except ValueError:
        logger.warning("Invalid TRIAL_DAYS value", extra={"value": raw})
        return 0


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


def _build_trial_message(days: int) -> str:
    return _safe_text(
        "Тестовый доступ доступен на 24 часа за 20⭐ в Telegram. Нажми «Получить новый ключ», и я подскажу, как оплатить."
    )


TRIAL_DAYS = _get_trial_days()


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


_qr_messages = _QrMessageTracker()
_qr_links = _QrLinkStorage()


class _SingleMessageManager:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._messages: dict[int, int] = {}

    async def send(
        self,
        source: Message,
        sender: Callable[[], Awaitable[Message]],
    ) -> Message:
        chat_id = source.chat.id
        async with self._lock:
            previous_id = self._messages.get(chat_id)
            if previous_id is not None:
                try:
                    await bot.delete_message(chat_id, previous_id)
                except Exception:
                    logger.debug(
                        "Failed to delete previous bot message",
                        extra={"chat_id": chat_id, "message_id": previous_id},
                    )
            message = await sender()
            self._messages[chat_id] = message.message_id
            return message

    async def remember(self, message: Message) -> None:
        async with self._lock:
            self._messages[message.chat.id] = message.message_id


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
            await _delete_previous_qr(chat_id)


async def _delete_previous_qr(chat_id: int) -> None:
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


async def send_single_message(
    message: Message,
    text: str,
    **kwargs: Any,
) -> Message:
    async def _send() -> Message:
        return await message.answer(text, **kwargs)

    return await _single_messages.send(message, _send)


_qr_cleanup_middleware = _QrCleanupMiddleware()
dp.message.middleware.register(_qr_cleanup_middleware)
dp.callback_query.middleware.register(_qr_cleanup_middleware)


def _load_api_urls() -> list[str]:
    raw_urls = os.getenv("VPN_API_URLS")
    if raw_urls:
        urls = [chunk.strip() for chunk in raw_urls.split(",") if chunk.strip()]
    else:
        single = os.getenv("VPN_API_URL")
        urls = [single.strip()] if single else []

    if not urls:
        urls = ["https://vpn-gpt.store/api", "http://127.0.0.1:8080"]

    normalized: list[str] = []
    for url in urls:
        if url:
            normalized.append(url.rstrip("/"))

    if not normalized:
        raise RuntimeError("Не удалось определить адреса API для VPN_GPT")

    return normalized


_VPN_API_URLS = _load_api_urls()
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "")
RENEW_DAYS = int(os.getenv("VPN_RENEW_DAYS", "30"))
_ALLOWED_BUTTON_SCHEMES = {"http", "https", "tg"}


def _build_action_buttons() -> list[list[InlineKeyboardButton]]:
    """Common set of action buttons shown under bot replies."""

    return [
        [
            InlineKeyboardButton(text="🔑 Получить новый ключ", callback_data="issue_key"),
            InlineKeyboardButton(text="♻️ Продлить доступ", callback_data="renew_key"),
        ],
        [InlineKeyboardButton(text="📄 Мой ключ", callback_data="get_key")],
        [InlineKeyboardButton(text="⬅️ Главное меню", callback_data="show_menu")],
    ]


def build_main_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔑 Получить новый ключ", callback_data="issue_key")],
            [InlineKeyboardButton(text="♻️ Продлить доступ", callback_data="renew_key")],
            [InlineKeyboardButton(text="📄 Мой ключ", callback_data="get_key")],
        ]
    )


def _is_supported_button_link(link: str) -> bool:
    """Return True when link is safe to use as a Telegram button URL."""

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

    # Telegram-specific deeplinks (tg://) may rely on path, netloc or query params.
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
    buttons.extend(_build_action_buttons())
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def format_key_info(payload: dict[str, Any], username: str, title: str) -> tuple[str, str | None]:
    lines: list[str] = [title]

    payload_username = payload.get("username")
    if payload_username:
        lines.append(f"Пользователь: {payload_username}")
    else:
        lines.append(f"Пользователь: {username}")

    uuid_value = payload.get("uuid")
    if uuid_value:
        lines.append(f"UUID: {uuid_value}")

    expires = payload.get("expires_at")
    if expires:
        lines.append(f"Действует до: {expires}")

    active = payload.get("active")
    if active is not None:
        status_text = "активен" if active else "неактивен"
        lines.append(f"Статус: {status_text}")

    link = payload.get("link")
    if link:
        lines.append("")
        lines.append("🔗 Ссылка для подключения:")
        lines.append(link)

    return _safe_text("\n".join(lines)), link


async def _api_request(
    method: str,
    path: str,
    *,
    params: dict[str, Any] | None = None,
    json_payload: dict[str, Any] | None = None,
    timeout: float = 10.0,
) -> dict[str, Any]:
    last_error: httpx.RequestError | None = None

    for base_url in _VPN_API_URLS:
        url = f"{base_url}{path}"
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.request(
                    method,
                    url,
                    params=params,
                    json=json_payload,
                )
        except httpx.RequestError as exc:
            last_error = exc
            logger.warning(
                "Failed to call VPN API",
                extra={
                    "url": url,
                    "method": method,
                    "error": str(exc),
                },
            )
            continue

        response.raise_for_status()
        return response.json()

    if last_error is None:
        raise RuntimeError("Не удалось выполнить запрос к VPN API: не задан ни один адрес")

    raise last_error


async def request_key(username: str) -> dict:
    params = {"x-admin-token": ADMIN_TOKEN} if ADMIN_TOKEN else None
    return await _api_request(
        "POST",
        "/vpn/issue_key",
        params=params,
        json_payload={"username": username},
    )


async def renew_key(username: str, days: int = RENEW_DAYS) -> dict:
    params = {"x-admin-token": ADMIN_TOKEN} if ADMIN_TOKEN else None
    return await _api_request(
        "POST",
        "/vpn/renew_key",
        params=params,
        json_payload={"username": username, "days": days},
    )


async def request_key_info(username: str, chat_id: int | None = None) -> dict:
    params: dict[str, Any] = {"username": username}
    if chat_id is not None:
        params["chat_id"] = chat_id

    return await _api_request("GET", "/vpn/my_key", params=params)


@dp.message(Command("start"))
async def start(msg: Message):
    await _delete_previous_qr(msg.chat.id)
    trial_message = _build_trial_message(TRIAL_DAYS)
    greeting = _safe_text(
        "👋 Привет! Я бот VPN_GPT. "
        f"{trial_message}\n"
        "\nВыбери действие в меню ниже, и я всё сделаю за тебя."
    )
    await send_single_message(msg, greeting, reply_markup=build_main_menu())


@dp.message(Command("buy"))
async def buy(msg: Message):
    username = msg.from_user.username or f"id_{msg.from_user.id}"
    await handle_issue_key(msg, username)


@dp.message(Command("mykey"))
async def my_key(msg: Message):
    username = msg.from_user.username or f"id_{msg.from_user.id}"
    await handle_get_key(msg, username, msg.chat.id)


@dp.message(Command("renew"))
async def renew(msg: Message):
    username = msg.from_user.username or f"id_{msg.from_user.id}"
    await handle_renew_key(msg, username, msg.chat.id)


async def handle_issue_key(message: Message, username: str) -> None:
    await _delete_previous_qr(message.chat.id)
    await send_single_message(
        message,
        _safe_text(
            "Сейчас тестовый доступ выдаётся за 20⭐ в Telegram. Открой основной бот @dobriyvpn_bot, оплати тест и получи ключ "
            "мгновенно. Если нужна помощь — просто напиши мне."
        ),
        reply_markup=build_result_markup(),
    )


async def handle_get_key(message: Message, username: str, chat_id: int) -> None:
    await _delete_previous_qr(message.chat.id)
    progress = await send_single_message(
        message,
        _safe_text("🔎 Проверяю информацию о твоём ключе…"),
        reply_markup=build_result_markup(),
    )

    try:
        payload = await request_key_info(username, chat_id=chat_id)
    except Exception:
        await progress.edit_text(
            _safe_text("⚠️ Не удалось получить информацию о ключе. Попробуй позже."),
            reply_markup=build_result_markup(),
        )
        return

    if not payload.get("ok"):
        await progress.edit_text(
            _safe_text(
                "ℹ️ Активный ключ не найден. Нажми кнопку \"Получить новый ключ\" в меню."
            ),
            reply_markup=build_result_markup(),
        )
        return

    text, link = format_key_info(payload, username, "🔐 Информация о твоём VPN-ключе:")
    await progress.edit_text(text, reply_markup=build_result_markup(link))

    if link:
        normalized_link = link.strip()
        if normalized_link:
            await _qr_links.remember(message.chat.id, normalized_link)


async def handle_renew_key(message: Message, username: str, chat_id: int) -> None:
    await _delete_previous_qr(message.chat.id)
    progress = await send_single_message(
        message,
        _safe_text("♻️ Продлеваю срок действия твоего ключа…"),
        reply_markup=build_result_markup(),
    )

    try:
        renew_payload = await renew_key(username)
    except Exception:
        await progress.edit_text(
            _safe_text("⚠️ Не удалось продлить доступ. Попробуй ещё раз позже."),
            reply_markup=build_result_markup(),
        )
        return

    if not renew_payload.get("ok"):
        detail = renew_payload.get("detail") or "Не удалось продлить доступ."
        await progress.edit_text(
            _safe_text(f"⚠️ {detail}"),
            reply_markup=build_result_markup(),
        )
        return

    try:
        info_payload = await request_key_info(username, chat_id=chat_id)
    except Exception:
        info_payload = None

    if info_payload and info_payload.get("ok"):
        text, link = format_key_info(info_payload, username, "♻️ Доступ успешно продлён!")
    else:
        expires = renew_payload.get("expires_at")
        lines = ["♻️ Доступ успешно продлён!"]
        if expires:
            lines.append(f"Новая дата окончания: {expires}")
        link = None
        text = _safe_text("\n".join(lines))

    await progress.edit_text(text, reply_markup=build_result_markup(link))

    if link:
        normalized_link = link.strip()
        if normalized_link:
            await _qr_links.remember(message.chat.id, normalized_link)


@dp.callback_query(F.data == "issue_key")
async def issue_key_callback(callback: CallbackQuery):
    await callback.answer()
    if not callback.message:
        return
    username = callback.from_user.username or f"id_{callback.from_user.id}"
    await handle_issue_key(callback.message, username)


@dp.callback_query(F.data == "renew_key")
async def renew_key_callback(callback: CallbackQuery):
    await callback.answer()
    if not callback.message:
        return
    username = callback.from_user.username or f"id_{callback.from_user.id}"
    await handle_renew_key(callback.message, username, callback.message.chat.id)


@dp.callback_query(F.data == "show_qr")
async def show_qr_callback(callback: CallbackQuery):
    if not callback.message:
        await callback.answer()
        return

    chat_id = callback.message.chat.id
    link = await _qr_links.get(chat_id)
    if not link:
        await callback.answer(_safe_text("QR недоступен"), show_alert=True)
        return

    await _delete_previous_qr(chat_id)

    qr = make_qr(link)
    qr_message = await callback.message.answer_photo(
        BufferedInputFile(qr.getvalue(), filename="vpn_key.png"),
        caption=_safe_text("📱 Отсканируй QR-код для быстрого подключения"),
    )
    normalized_link = link.strip()
    if normalized_link:
        await _qr_links.remember(chat_id, normalized_link)
    await _qr_messages.remember(chat_id, qr_message.message_id)
    await callback.answer()


@dp.callback_query(F.data == "get_key")
async def get_key_callback(callback: CallbackQuery):
    await callback.answer()
    if not callback.message:
        return
    username = callback.from_user.username or f"id_{callback.from_user.id}"
    await handle_get_key(callback.message, username, callback.message.chat.id)


@dp.callback_query(F.data == "show_menu")
async def show_menu(callback: CallbackQuery):
    await callback.answer()
    if not callback.message:
        return
    await _delete_previous_qr(callback.message.chat.id)
    await send_single_message(
        callback.message,
        _safe_text("Выбери нужное действие:"),
        reply_markup=build_main_menu(),
    )


async def main():
    try:
        await bot.delete_my_commands()
        await bot.set_chat_menu_button(MenuButtonDefault())
    except Exception:
        # Для простого бота ограничимся сообщением в stdout.
        print(_safe_text("⚠️ Не удалось очистить меню команд бота"), flush=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())

