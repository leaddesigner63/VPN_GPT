from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import json
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import httpx
from collections.abc import Mapping, Sequence

from api.utils.logging import get_logger

logger = get_logger("integrations.morune")

_URL_RE = re.compile(r"https?://[^\s\"'<>]+", re.IGNORECASE)
_SCHEMELESS_URL_RE = re.compile(r"(?:(?:https?:)?//)[^\s\"'<>]+", re.IGNORECASE)
_RELATIVE_PATH_RE = re.compile(r"(?:(?<=^)|(?<=\s))/[^\s\"'<>]+")
_MORUNE_DOMAIN_RE = re.compile(
    r"\b(?:[a-z0-9-]+\.)*morune\.[a-z]{2,}(?:/[^\s\"'<>]*)?",
    re.IGNORECASE,
)


class MoruneError(RuntimeError):
    """Base exception for Morune integration errors."""


class MoruneConfigurationError(MoruneError):
    """Raised when required configuration is missing."""


class MoruneAPIError(MoruneError):
    """Raised when Morune API replies with an error."""


class MoruneSignatureError(MoruneError):
    """Raised when webhook signature validation fails."""


@dataclass(slots=True)
class MoruneInvoice:
    payment_id: str
    provider_payment_id: str | None
    payment_url: str | None
    status: str
    amount: int | None
    currency: str | None
    raw: dict[str, Any]


@dataclass(slots=True)
class MoruneWebhookEvent:
    payment_id: str
    provider_payment_id: str | None
    status: str
    amount: int | None
    currency: str | None
    paid_at: dt.datetime | None
    raw: dict[str, Any]


def _parse_amount(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, (float,)):
        return int(round(value))
    text = str(value).strip()
    if not text:
        return None
    try:
        if "." in text:
            return int(round(float(text)))
        return int(text)
    except ValueError:
        logger.warning("Failed to parse Morune amount", extra={"value": value})
        return None


def _parse_datetime(value: Any) -> dt.datetime | None:
    if not value:
        return None
    if isinstance(value, (int, float)):
        return dt.datetime.utcfromtimestamp(float(value)).replace(microsecond=0)
    text = str(value).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = dt.datetime.fromisoformat(text)
    except ValueError:
        logger.warning("Failed to parse Morune datetime", extra={"value": value})
        return None
    if parsed.tzinfo:
        parsed = parsed.astimezone(dt.timezone.utc).replace(tzinfo=None)
    return parsed.replace(microsecond=0)


def _normalise_key(value: str) -> str:
    return "".join(ch for ch in value.lower() if ch.isalnum())


def _is_sequence(value: Any) -> bool:
    return isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray))


def _search_nested_value(payload: Any, keys: Sequence[str]) -> Any | None:
    """Return the first non-empty value for keys inside ``payload``.

    Morune менял структуру ответа несколько раз: поля могли оказаться внутри
    ``data.attributes``, ``links.checkout`` или других вложенных словарей.
    Чтобы не завязываться на конкретной схеме, обходим структуру в глубину и
    возвращаем первое осмысленное значение.
    """

    if payload is None:
        return None

    target_keys = {_normalise_key(key) for key in keys}
    skip_containers = {_normalise_key(name) for name in ("metadata", "meta")}
    stack: list[Any] = [payload]
    seen: set[int] = set()

    while stack:
        current = stack.pop()
        ident = id(current)
        if ident in seen:
            continue
        seen.add(ident)

        if isinstance(current, Mapping):
            for raw_key, value in current.items():
                norm_key = _normalise_key(str(raw_key))
                if norm_key in target_keys and value not in (None, ""):
                    if isinstance(value, Mapping) or _is_sequence(value):
                        stack.append(value)
                        continue
                    return value
            for raw_key, value in current.items():
                norm_key = _normalise_key(str(raw_key))
                if norm_key in skip_containers:
                    continue
                if isinstance(value, Mapping) or _is_sequence(value):
                    stack.append(value)
        elif _is_sequence(current):
            for item in reversed(list(current)):
                if isinstance(item, Mapping) or _is_sequence(item):
                    stack.append(item)

    return None


def _stringify(value: Any, *, extra_keys: Sequence[str] | None = None) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        text = value.strip()
        return text or None
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, Mapping) or _is_sequence(value):
        keys = ["url", "href", "link", "value"]
        if extra_keys:
            keys.extend(extra_keys)
        nested = _search_nested_value(value, keys)
        if nested is None or nested is value:
            return None
        return _stringify(nested, extra_keys=extra_keys)
    return str(value).strip() or None


def _extract_first_url(payload: Any) -> str | None:
    """Return the first ``http`` URL-like string found inside ``payload``.

    Morune периодически меняет схему ответа и переименовывает поля с ссылкой
    на оплату. Если ни один из известных ключей не подошёл, просканируем
    вложенные структуры и достанем первую строку, похожую на URL.
    """

    if payload is None:
        return None

    stack: list[Any] = [payload]
    seen: set[int] = set()

    while stack:
        current = stack.pop()
        ident = id(current)
        if ident in seen:
            continue
        seen.add(ident)

        if isinstance(current, Mapping):
            values = list(current.values())
            for value in values:
                if isinstance(value, str):
                    candidate = value.strip()
                    if not candidate:
                        continue
                    url_candidate = _detect_url_candidate(candidate)
                    if url_candidate:
                        return url_candidate
            for value in reversed(values):
                if isinstance(value, Mapping) or _is_sequence(value):
                    stack.append(value)
        elif _is_sequence(current):
            for item in current:
                if isinstance(item, str):
                    candidate = item.strip()
                    if not candidate:
                        continue
                    url_candidate = _detect_url_candidate(candidate)
                    if url_candidate:
                        return url_candidate
            for item in reversed(list(current)):
                if isinstance(item, Mapping) or _is_sequence(item):
                    stack.append(item)

    return None


def _detect_url_candidate(text: str) -> str | None:
    if not text:
        return None
    stripped = text.strip()
    if stripped.startswith(("http://", "https://", "//")) and stripped.strip("/"):
        return stripped
    if stripped.startswith("/") and stripped.strip("/"):
        return stripped
    match = _URL_RE.search(text)
    if match:
        return match.group(0)
    match = _SCHEMELESS_URL_RE.search(text)
    if match:
        candidate = match.group(0)
        if candidate.strip("/"):
            return candidate
    match = _RELATIVE_PATH_RE.search(text)
    if match:
        candidate = match.group(0)
        if candidate.strip("/"):
            return candidate
    match = _MORUNE_DOMAIN_RE.search(text)
    if match:
        candidate = match.group(0)
        if candidate:
            return candidate
    return None


def _coerce_url(candidate: str | None, *, base_url: str | None = None) -> str | None:
    if not candidate:
        return None
    text = str(candidate).strip()
    if not text:
        return None
    if text.startswith("http://") or text.startswith("https://"):
        return text
    if text.startswith("//"):
        return "https:" + text
    if text.startswith("/"):
        if base_url:
            parsed = urlparse(base_url)
            scheme = parsed.scheme or "https"
            netloc = parsed.netloc
            if netloc:
                return f"{scheme}://{netloc}{text}"
        return None
    match = _URL_RE.search(text)
    if match:
        return match.group(0)
    if text.lower().startswith("www."):
        return f"https://{text}"
    if "morune" in text.lower() and " " not in text:
        stripped = text.lstrip("/")
        return f"https://{stripped}" if not stripped.startswith("http") else stripped
    parsed = urlparse(text)
    if parsed.scheme in ("http", "https") and parsed.netloc:
        return text
    if parsed.netloc and not parsed.scheme:
        return f"https://{text}"
    if "." in text and " " not in text:
        return f"https://{text}"
    return None


class MoruneClient:
    """Minimal HTTP client for Morune payment API."""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        shop_id: str,
        webhook_secret: str | None = None,
        timeout: float = 15.0,
    ) -> None:
        if not base_url or not api_key or not shop_id:
            raise MoruneConfigurationError("Morune configuration is incomplete")
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.shop_id = shop_id
        self.webhook_secret = webhook_secret
        self.timeout = timeout
        self._client = httpx.Client(timeout=timeout)

    def close(self) -> None:
        self._client.close()

    def _headers(self) -> dict[str, str]:
        token = self.api_key.strip()
        return {
            "X-Api-Key": token,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def _request(
        self,
        method: str,
        path: str,
        *,
        json_payload: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        url = f"{self.base_url}{path}"
        try:
            response = self._client.request(
                method,
                url,
                json=json_payload,
                headers=self._headers(),
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:  # pragma: no cover - network errors
            logger.exception("Morune API request failed", extra={"url": url, "error": str(exc)})
            raise MoruneAPIError(str(exc)) from exc
        try:
            return response.json()
        except ValueError as exc:  # pragma: no cover - invalid JSON
            logger.exception("Morune API returned invalid JSON", extra={"url": url})
            raise MoruneAPIError("invalid_json") from exc

    def create_invoice(
        self,
        *,
        payment_id: str,
        amount: int,
        currency: str,
        description: str,
        metadata: Mapping[str, Any] | None,
        success_url: str | None,
        fail_url: str | None,
    ) -> MoruneInvoice:
        payload: dict[str, Any] = {
            "shop_id": self.shop_id,
            "amount": amount,
            "currency": currency,
            "order_id": payment_id,
            "description": description,
        }
        if metadata:
            payload["metadata"] = dict(metadata)
        if success_url:
            payload["success_url"] = success_url
        if fail_url:
            payload["fail_url"] = fail_url

        data = self._request("POST", "/invoice/create", json_payload=payload)
        extracted = self._extract_invoice_fields(
            payload=data,
            payment_id=payment_id,
            fallback_currency=currency,
        )

        provider_payment_id = extracted["provider_payment_id"]
        payment_url = extracted["payment_url"]
        status = extracted["status"]
        amount_value = extracted["amount"]
        currency_value = extracted["currency"]

        if not provider_payment_id:
            logger.warning("Morune invoice missing provider payment id", extra={"payment_id": payment_id})
        if not payment_url:
            logger.warning("Morune invoice missing payment URL", extra={"payment_id": payment_id})

        return MoruneInvoice(
            payment_id=payment_id,
            provider_payment_id=str(provider_payment_id) if provider_payment_id else None,
            payment_url=payment_url,
            status=status,
            amount=amount_value,
            currency=currency_value,
            raw=data,
        )

    def _extract_invoice_fields(
        self,
        *,
        payload: Mapping[str, Any],
        payment_id: str,
        fallback_currency: str | None,
    ) -> dict[str, Any]:
        data = payload
        invoice = payload.get("data") or payload
        provider_payment_id = (
            invoice.get("id")
            or invoice.get("payment_id")
            or invoice.get("uuid")
            or _search_nested_value(
                invoice,
                [
                    "payment_id",
                    "paymentId",
                    "invoice_id",
                    "invoiceId",
                    "order_id",
                    "orderId",
                    "uuid",
                    "id",
                    "hash",
                    "payment_hash",
                    "paymentHash",
                    "invoice_hash",
                    "invoiceHash",
                ],
            )
            or _search_nested_value(
                data,
                [
                    "payment_id",
                    "paymentId",
                    "invoice_id",
                    "invoiceId",
                    "order_id",
                    "orderId",
                    "uuid",
                    "id",
                    "hash",
                    "payment_hash",
                    "paymentHash",
                    "invoice_hash",
                    "invoiceHash",
                ],
            )
        )
        payment_url = (
            invoice.get("url")
            or invoice.get("payment_url")
            or invoice.get("redirect_url")
            or invoice.get("invoice_url")
            or invoice.get("cashier_url")
            or invoice.get("payment_link")
            or _search_nested_value(
                invoice,
                [
                    "payment_url",
                    "paymentUrl",
                    "redirect_url",
                    "redirectUrl",
                    "checkout_url",
                    "checkoutUrl",
                    "pay_url",
                    "payUrl",
                    "payment_link",
                    "paymentLink",
                    "invoice_url",
                    "invoiceUrl",
                    "cashier_url",
                    "cashierUrl",
                    "iframe_url",
                    "iframeUrl",
                    "url",
                    "href",
                    "link",
                    "page",
                ],
            )
            or _search_nested_value(
                data,
                [
                    "payment_url",
                    "paymentUrl",
                    "redirect_url",
                    "redirectUrl",
                    "checkout_url",
                    "checkoutUrl",
                    "pay_url",
                    "payUrl",
                    "payment_link",
                    "paymentLink",
                    "invoice_url",
                    "invoiceUrl",
                    "cashier_url",
                    "cashierUrl",
                    "iframe_url",
                    "iframeUrl",
                    "url",
                    "href",
                    "link",
                    "page",
                ],
            )
        )
        if not payment_url:
            payment_url = _extract_first_url(invoice) or _extract_first_url(data)
        status = (
            invoice.get("status")
            or data.get("status")
            or _search_nested_value(invoice, ["status", "state"])
            or _search_nested_value(data, ["status", "state"])
            or "pending"
        )
        amount_value = (
            invoice.get("amount")
            or invoice.get("total")
            or data.get("amount")
            or data.get("total")
            or _search_nested_value(invoice, ["amount", "total", "sum", "value"])
            or _search_nested_value(data, ["amount", "total", "sum", "value"])
        )
        raw_currency = (
            invoice.get("currency")
            or data.get("currency")
            or _search_nested_value(invoice, ["currency", "currency_code", "curr"])
            or _search_nested_value(data, ["currency", "currency_code", "curr"])
            or fallback_currency
            or ""
        )
        if isinstance(raw_currency, str):
            stripped_currency = raw_currency.strip()
            currency_value = stripped_currency.upper() if stripped_currency else None
        else:
            coerced_currency = _stringify(raw_currency, extra_keys=["code", "currency"])
            currency_value = coerced_currency.upper() if coerced_currency else None

        if not currency_value and fallback_currency:
            currency_value = fallback_currency.strip().upper() or None

        provider_payment_id = _stringify(provider_payment_id)
        payment_url = _stringify(payment_url, extra_keys=["checkout", "payment"])
        payment_url = _coerce_url(payment_url, base_url=self.base_url)
        status = str(status).lower() if status else "pending"

        return {
            "provider_payment_id": provider_payment_id,
            "payment_url": payment_url,
            "status": status,
            "amount": _parse_amount(amount_value),
            "currency": currency_value,
        }

    @staticmethod
    def _merge_invoice_fields(target: dict[str, Any], source: dict[str, Any]) -> None:
        for key in ("provider_payment_id", "payment_url", "status", "currency"):
            value = source.get(key)
            if value:
                target[key] = value
        amount_value = source.get("amount")
        if amount_value is not None:
            target["amount"] = amount_value

    def verify_signature(self, *, body: bytes, signature: str | None) -> None:
        if not self.webhook_secret:
            raise MoruneConfigurationError("Webhook secret is not configured")
        if not signature:
            raise MoruneSignatureError("missing_signature")
        digest = hmac.new(self.webhook_secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(digest, signature):
            logger.warning("Morune webhook signature mismatch")
            raise MoruneSignatureError("invalid_signature")

    def parse_webhook(self, *, body: bytes, signature: str | None) -> MoruneWebhookEvent:
        if self.webhook_secret:
            self.verify_signature(body=body, signature=signature)
        try:
            payload = json.loads(body.decode("utf-8"))
        except json.JSONDecodeError as exc:
            logger.exception("Failed to decode Morune webhook payload")
            raise MoruneAPIError("invalid_webhook_json") from exc

        data = payload.get("data") or payload
        metadata = data.get("metadata") or data.get("meta") or payload.get("meta") or {}
        payment_id = (
            data.get("order_id")
            or data.get("orderId")
            or data.get("order")
            or data.get("external_id")
            or metadata.get("order_id")
            or metadata.get("payment_id")
        )
        if not payment_id:
            logger.error("Morune webhook missing payment identifier", extra={"payload": payload})
            raise MoruneAPIError("payment_id_missing")

        provider_payment_id = data.get("id") or data.get("payment_id") or data.get("invoice_id")
        status = (data.get("status") or payload.get("status") or payload.get("event") or "unknown").lower()
        amount_value = data.get("amount") or data.get("total") or payload.get("amount")
        currency_value = (data.get("currency") or payload.get("currency") or "").upper() or None
        paid_at_value = (
            data.get("paid_at")
            or data.get("paidAt")
            or data.get("confirmed_at")
            or metadata.get("paid_at")
            or payload.get("paid_at")
        )

        return MoruneWebhookEvent(
            payment_id=str(payment_id),
            provider_payment_id=str(provider_payment_id) if provider_payment_id else None,
            status=str(status),
            amount=_parse_amount(amount_value),
            currency=currency_value,
            paid_at=_parse_datetime(paid_at_value),
            raw=payload,
        )


__all__ = [
    "MoruneClient",
    "MoruneInvoice",
    "MoruneWebhookEvent",
    "MoruneError",
    "MoruneAPIError",
    "MoruneConfigurationError",
    "MoruneSignatureError",
]
