from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from aiogram import F, Dispatcher, Router
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LabeledPrice,
    Message,
    PreCheckoutQuery,
)

from api.utils.telegram import TelegramInvoiceError, create_invoice_link
from utils.stars import (
    StarPlan,
    StarSettings,
    build_invoice_payload,
    build_invoice_request_data,
)

STAR_CALLBACK_PREFIX = "stars:buy:"

router = Router(name="stars")
@dataclass(slots=True)
class StarHandlerDependencies:
    settings: StarSettings
    pay_prefix: str
    build_result_markup: Callable[[str | None], InlineKeyboardMarkup]
    remember_qr: Callable[[int, str], Awaitable[None]]
    delete_previous_qr: Callable[[int], Awaitable[None]]
    format_key_info: Callable[[dict[str, Any], str, str], tuple[str, str | None]]
    register_user: Callable[[str, int | None, str | None], Awaitable[None]]
    renew_access: Callable[[str, str, int | None], Awaitable[dict[str, Any]]]
    create_payment_record: Callable[..., Awaitable[dict]]
    get_payment_by_charge: Callable[[str], Awaitable[dict | None]]
    mark_payment_pending: Callable[[int, str | None], Awaitable[dict | None]]
    mark_payment_fulfilled: Callable[[int], Awaitable[dict | None]]
    list_pending_payments: Callable[[str], Awaitable[list[dict]]]
    logger: logging.Logger


_deps: StarHandlerDependencies | None = None
_pending_locks: dict[str, asyncio.Lock] = {}


def setup_stars_handlers(dp: Dispatcher, deps: StarHandlerDependencies) -> None:
    global _deps
    _deps = deps
    dp.include_router(router)
    deps.logger.info(
        "Star payments handler initialised",
        extra={"enabled": deps.settings.enabled, "subscription": deps.settings.subscription_enabled},
    )


def _get_deps() -> StarHandlerDependencies:
    if _deps is None:
        raise RuntimeError("Star handler dependencies not configured")
    return _deps


def _resolve_plan(plan_code: str) -> tuple[StarPlan | None, bool]:
    deps = _get_deps()
    settings = deps.settings
    plan = settings.plans.get(plan_code)
    if plan:
        return plan, plan.is_subscription
    sub_plan = settings.subscription_plan if settings.subscription_enabled else None
    if sub_plan and plan_code == sub_plan.code:
        return sub_plan, True
    return None, False


async def _handle_duplicate_payment(message: Message, username: str) -> None:
    deps = _get_deps()
    await deps.delete_previous_qr(message.chat.id)
    text = "⭐️ Этот платёж уже обработан. Открой раздел «Мои ключи», чтобы увидеть актуальный доступ."
    markup = deps.build_result_markup(None)
    await message.answer(text, reply_markup=markup)


def _build_invoice_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="⬅️ Главное меню", callback_data="menu_back")]]
    )


def _build_invoice_link_markup(link: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="⭐️ Оформить подписку", url=link)],
            [InlineKeyboardButton(text="⬅️ Главное меню", callback_data="menu_back")],
        ]
    )


async def _deliver_payment(
    message: Message,
    *,
    plan: StarPlan,
    username: str,
    payment_record: dict,
) -> bool:
    deps = _get_deps()
    chat_id = message.chat.id
    try:
        api_payload = await deps.renew_access(username, plan.code, chat_id)
    except Exception as exc:  # pragma: no cover - defensive logging
        deps.logger.exception(
            "Failed to renew VPN access after Stars payment",
            extra={"username": username, "plan": plan.code, "error": str(exc)},
        )
        if payment_record.get("id") is not None:
            await deps.mark_payment_pending(int(payment_record["id"]), error=str(exc))
        await message.answer(
            "⭐️ Оплата получена, но сейчас выдача доступа задерживается. "
            "Мы пришлём ключ, как только сервис восстановится."
        )
        return False

    if payment_record.get("id") is not None:
        await deps.mark_payment_fulfilled(int(payment_record["id"]))

    title = "⭐️ Доступ активирован!"
    text, link = deps.format_key_info(api_payload, username, title)
    await deps.delete_previous_qr(chat_id)
    await message.answer(text, reply_markup=deps.build_result_markup(link))
    if link:
        clean_link = link.strip()
        if clean_link:
            await deps.remember_qr(chat_id, clean_link)
    deps.logger.info(
        "Delivered VPN access for Stars payment",
        extra={"username": username, "plan": plan.code, "payment_id": payment_record.get("id")},
    )
    return True


@router.callback_query(F.data.startswith(STAR_CALLBACK_PREFIX))
async def handle_star_purchase(callback: CallbackQuery) -> None:
    deps = _get_deps()
    if not deps.settings.enabled:
        await callback.answer("Оплата звёздами временно недоступна", show_alert=True)
        return

    data = callback.data or ""
    plan_code = data[len(deps.pay_prefix) :]
    plan, _ = _resolve_plan(plan_code)
    if plan is None:
        deps.logger.warning("User requested unknown Stars plan", extra={"code": plan_code})
        await callback.answer("Тариф недоступен. Попробуй выбрать другой вариант.", show_alert=True)
        return

    message = callback.message
    user = callback.from_user
    if message is None or user is None:
        await callback.answer()
        return

    chat_id = message.chat.id
    username = user.username or f"id_{user.id}"
    await deps.delete_previous_qr(chat_id)
    await deps.register_user(username, chat_id, user.username)

    description = f"Доступ к VPN_GPT на {plan.title.lower()}"
    if plan.is_subscription:
        description = f"Подписка VPN_GPT на {plan.title.lower()} с автопродлением"

    if plan.is_subscription:
        invoice_payload = build_invoice_request_data(plan)
        try:
            link = await create_invoice_link(invoice_payload)
        except TelegramInvoiceError as exc:
            deps.logger.exception(
                "Failed to request Telegram Stars invoice link",
                extra={"plan": plan.code, "error": exc.detail},
            )
            await callback.answer("Не удалось создать счёт. Попробуй ещё раз позже.", show_alert=True)
            return

        message_text = (
            "⭐️ Открой ссылку ниже, чтобы оформить подписку. "
            "После оплаты мы мгновенно выдадим доступ."
        )
        await message.answer(message_text, reply_markup=_build_invoice_link_markup(link))
        deps.logger.info(
            "Sent Stars invoice link",
            extra={"plan": plan.code, "chat_id": chat_id},
        )
        await callback.answer()
        return

    try:
        invoice_message = await message.answer_invoice(
            title=f"VPN_GPT · {plan.title}",
            description=description,
            currency="XTR",
            prices=[LabeledPrice(label=plan.label, amount=plan.price_stars)],
            payload=build_invoice_payload(plan.code),
            reply_markup=_build_invoice_markup(),
        )
    except Exception as exc:  # pragma: no cover - defensive
        deps.logger.exception(
            "Failed to send Stars invoice",
            extra={"plan": plan.code, "error": str(exc)},
        )
        await callback.answer("Не удалось создать счёт. Попробуй ещё раз позже.", show_alert=True)
        return

    deps.logger.info(
        "Sent Stars invoice",
        extra={"plan": plan.code, "chat_id": chat_id, "message_id": invoice_message.message_id},
    )
    await callback.answer()


@router.pre_checkout_query()
async def handle_pre_checkout(query: PreCheckoutQuery) -> None:
    deps = _get_deps()
    payload = query.invoice_payload or ""
    if not payload.startswith(deps.pay_prefix):
        await query.answer(ok=True)
        return

    plan_code = payload[len(deps.pay_prefix) :]
    plan, _ = _resolve_plan(plan_code)
    if plan is None or not deps.settings.enabled:
        await query.answer(ok=False, error_message="Тариф недоступен. Попробуйте позже.")
        deps.logger.warning("Pre-checkout rejected", extra={"plan": plan_code, "enabled": deps.settings.enabled})
        return

    try:
        await query.answer(ok=True)
    except Exception as exc:  # pragma: no cover - defensive
        deps.logger.exception("Failed to answer pre_checkout_query", extra={"error": str(exc)})


@router.message(F.successful_payment)
async def handle_successful_payment(message: Message) -> None:
    deps = _get_deps()
    payment = message.successful_payment
    if payment is None or payment.currency != "XTR":
        return

    payload = payment.invoice_payload or ""
    if not payload.startswith(deps.pay_prefix):
        return

    plan_code = payload[len(deps.pay_prefix) :]
    plan, is_subscription = _resolve_plan(plan_code)
    if plan is None:
        deps.logger.error("Received Stars payment for unknown plan", extra={"payload": payload})
        await message.answer("⭐️ Платёж принят, но тариф не распознан. Поддержка уже уведомлена.")
        return

    user = message.from_user
    if user is None:
        deps.logger.warning("Successful Stars payment without user context")
        return

    username = user.username or f"id_{user.id}"
    chat_id = message.chat.id
    await deps.register_user(username, chat_id, user.username)

    charge_id = payment.telegram_payment_charge_id or ""
    existing = await deps.get_payment_by_charge(charge_id) if charge_id else None
    if existing and not existing.get("delivery_pending") and existing.get("fulfilled_at"):
        await _handle_duplicate_payment(message, username)
        return

    if existing:
        payment_record = existing
    else:
        payment_record = await deps.create_payment_record(
            user_id=user.id,
            username=username,
            plan=plan.code,
            amount_stars=payment.total_amount,
            charge_id=charge_id or None,
            is_subscription=is_subscription or bool(payment.subscription_id),
            status="paid",
            delivery_pending=False,
        )

    await _deliver_payment(message, plan=plan, username=username, payment_record=payment_record)


async def process_pending_deliveries(message: Message, username: str) -> None:
    deps = _get_deps()
    if not deps.settings.enabled:
        return

    lock = _pending_locks.setdefault(username, asyncio.Lock())
    if lock.locked():
        return

    async with lock:
        pending = await deps.list_pending_payments(username)
        if not pending:
            return
        deps.logger.info(
            "Processing pending Stars deliveries",
            extra={"username": username, "count": len(pending)},
        )
        for record in pending:
            plan_code = record.get("plan") or ""
            plan, _ = _resolve_plan(plan_code)
            if plan is None:
                deps.logger.warning(
                    "Skipping pending Stars delivery due to unknown plan",
                    extra={"plan": plan_code, "payment_id": record.get("id")},
                )
                continue
            await _deliver_payment(message, plan=plan, username=username, payment_record=record)
