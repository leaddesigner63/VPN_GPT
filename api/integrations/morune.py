from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import json
from dataclasses import dataclass
from typing import Any, Mapping

import httpx

from api.utils.logging import get_logger

logger = get_logger("integrations.morune")


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


class MoruneClient:
    """Minimal HTTP client for Morune payment API."""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        project_id: str,
        webhook_secret: str | None = None,
        timeout: float = 15.0,
    ) -> None:
        if not base_url or not api_key or not project_id:
            raise MoruneConfigurationError("Morune configuration is incomplete")
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.project_id = project_id
        self.webhook_secret = webhook_secret
        self.timeout = timeout
        self._client = httpx.Client(timeout=timeout)

    def close(self) -> None:
        self._client.close()

    def _headers(self) -> dict[str, str]:
        token = self.api_key.strip()
        return {
            "Authorization": f"Bearer {token}",
            "X-Api-Key": token,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def _request(self, method: str, path: str, *, json_payload: Mapping[str, Any]) -> dict[str, Any]:
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
            "project_id": self.project_id,
            "amount": amount,
            "currency": currency,
            "order_id": payment_id,
            "description": description,
            "metadata": dict(metadata or {}),
        }
        if success_url:
            payload["success_url"] = success_url
        if fail_url:
            payload["fail_url"] = fail_url

        data = self._request("POST", "/e/api/invoices", json_payload=payload)
        invoice = data.get("data") or data
        provider_payment_id = invoice.get("id") or invoice.get("payment_id") or invoice.get("uuid")
        payment_url = invoice.get("url") or invoice.get("payment_url") or invoice.get("redirect_url")
        status = invoice.get("status") or data.get("status") or "pending"
        amount_value = invoice.get("amount") or invoice.get("total") or data.get("amount")
        currency_value = (invoice.get("currency") or data.get("currency") or currency or "").upper() or None

        if not provider_payment_id:
            logger.warning("Morune invoice missing provider payment id", extra={"payment_id": payment_id})
        if not payment_url:
            logger.warning("Morune invoice missing payment URL", extra={"payment_id": payment_id})

        return MoruneInvoice(
            payment_id=payment_id,
            provider_payment_id=str(provider_payment_id) if provider_payment_id else None,
            payment_url=str(payment_url) if payment_url else None,
            status=str(status).lower(),
            amount=_parse_amount(amount_value),
            currency=currency_value,
            raw=data,
        )

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
