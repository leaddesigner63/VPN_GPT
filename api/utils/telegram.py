from __future__ import annotations

import os
from typing import Any

import httpx

from api.utils.logging import get_logger
from utils.content_filters import assert_no_geoblocking, sanitize_text
from utils.stars import (
    STAR_PAYLOAD_PREFIX,
    build_invoice_payload,
    extract_plan_code_from_payload,
)

BOT_TOKEN = os.getenv("BOT_TOKEN")
BASE = os.getenv("BASE_BOT_URL", "https://api.telegram.org")
logger = get_logger("utils.telegram")


class TelegramInvoiceError(RuntimeError):
    def __init__(self, detail: str, *, status_code: int | None = None) -> None:
        super().__init__(detail)
        self.detail = detail
        self.status_code = status_code


def _url(method: str) -> str:
    return f"{BASE}/bot{BOT_TOKEN}/{method}"


def _prepare_invoice_payload(raw_payload: dict[str, Any]) -> dict[str, Any]:
    prepared = dict(raw_payload)
    for field in ("title", "description"):
        value = prepared.get(field)
        if isinstance(value, str):
            cleaned = sanitize_text(value)
            assert_no_geoblocking(cleaned)
            prepared[field] = cleaned
    return prepared


async def _post_invoice_request(url: str, payload: dict[str, Any]) -> httpx.Response:
    async with httpx.AsyncClient(timeout=20) as client:
        return await client.post(url, json=payload)


def _analyse_invoice_error(
    exc: httpx.HTTPStatusError,
    payload: dict[str, Any],
    duplicate_attempts: int,
    subscription_trimmed: bool,
) -> tuple[str, str | None, str | None]:
    description = ""
    try:
        response_json = exc.response.json()
    except ValueError:
        response_json = None
    if isinstance(response_json, dict):
        description = str(response_json.get("description") or "")
    if not description:
        description = exc.response.text

    lowered = description.lower()

    if (
        "payload" in payload
        and "payload" in lowered
        and "not unique" in lowered
        and duplicate_attempts < 3
    ):
        plan_code, _ = extract_plan_code_from_payload(payload.get("payload", ""), prefix=STAR_PAYLOAD_PREFIX)
        if plan_code:
            return description, "regenerate_payload", plan_code

    if (
        not subscription_trimmed
        and "subscription" in lowered
        and "invalid" in lowered
        and "subscription_period" in payload
    ):
        return description, "drop_subscription_period", None

    return description, None, None


async def create_invoice_link(payload: dict[str, Any]) -> str:
    if not BOT_TOKEN:
        logger.error("Cannot create invoice link: BOT_TOKEN is not configured")
        raise TelegramInvoiceError("telegram_token_missing", status_code=500)

    url = _url("createInvoiceLink")
    attempt_payload = _prepare_invoice_payload(payload)
    duplicate_attempts = 0
    subscription_trimmed = False
    try:
        while True:
            response = await _post_invoice_request(url, attempt_payload)
            try:
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:  # pragma: no cover - handled below
                description, action, context = _analyse_invoice_error(
                    exc, attempt_payload, duplicate_attempts, subscription_trimmed
                )
                if action == "regenerate_payload":
                    duplicate_attempts += 1
                    if not context:
                        logger.exception(
                            "Telegram returned HTTP error for createInvoiceLink",
                            extra={
                                "status": exc.response.status_code,
                                "body": exc.response.text,
                                "description": description,
                            },
                        )
                        raise TelegramInvoiceError(
                            "telegram_http_error", status_code=exc.response.status_code
                        ) from exc
                    attempt_payload = dict(attempt_payload)
                    attempt_payload["payload"] = build_invoice_payload(context)
                    continue
                if action == "drop_subscription_period":
                    subscription_trimmed = True
                    attempt_payload = dict(attempt_payload)
                    attempt_payload.pop("subscription_period", None)
                    continue
                logger.exception(
                    "Telegram returned HTTP error for createInvoiceLink",
                    extra={
                        "status": exc.response.status_code,
                        "body": exc.response.text,
                        "description": description,
                    },
                )
                raise TelegramInvoiceError(
                    "telegram_http_error", status_code=exc.response.status_code
                ) from exc
            else:
                break
    except httpx.RequestError as exc:
        logger.exception("Failed to call Telegram createInvoiceLink", extra={"error": str(exc)})
        raise TelegramInvoiceError("telegram_request_failed") from exc

    try:
        data = response.json()
    except ValueError as exc:  # pragma: no cover - defensive logging
        logger.exception(
            "Telegram createInvoiceLink returned non-JSON response",
            extra={"status": response.status_code, "body": response.text},
        )
        raise TelegramInvoiceError("telegram_invalid_response") from exc

    if not data.get("ok"):
        logger.error("Telegram createInvoiceLink returned failure", extra={"response": data})
        raise TelegramInvoiceError("telegram_error")

    result = data.get("result")
    if not isinstance(result, str):
        logger.error("Unexpected Telegram response payload", extra={"result": result})
        raise TelegramInvoiceError("telegram_invalid_response")

    return result


async def send_message(chat_id: int | str, text: str, parse_mode: str | None = None):
    logger.info("Sending Telegram message", extra={"chat_id": chat_id})
    safe_text = sanitize_text(text)
    assert_no_geoblocking(safe_text)
    async with httpx.AsyncClient(timeout=30) as client:
        data = {"chat_id": chat_id, "text": safe_text}
        if parse_mode:
            data["parse_mode"] = parse_mode
        r = await client.post(_url("sendMessage"), json=data)
        r.raise_for_status()
        logger.debug("Telegram API response", extra={"chat_id": chat_id, "status_code": r.status_code})
        return r.json()


async def broadcast(chat_ids: list[int | str], text: str):
    results = []
    for cid in chat_ids:
        try:
            results.append(await send_message(cid, text))
        except Exception as e:
            logger.exception("Failed to send Telegram message", extra={"chat_id": cid})
            results.append({"chat_id": cid, "ok": False, "error": str(e)})
        else:
            logger.info("Telegram message sent successfully", extra={"chat_id": cid})
    return results
