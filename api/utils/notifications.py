from __future__ import annotations

import asyncio
import os
import threading
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Sequence

from openai import OpenAI

from api import config
from api.utils import db
from api.utils.logging import get_logger
from api.utils.telegram import send_message as telegram_send_message
from utils.stars import StarPlan, StarSettings

logger = get_logger("renewal.notifications")


def _parse_float_env(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        value = float(raw.strip())
    except ValueError:
        logger.warning(
            "Invalid float value supplied for env; using default",
            extra={"env_name": name, "env_value": raw, "default": default},
        )
        return default
    return value


def _parse_int_env(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        value = int(raw.strip())
    except ValueError:
        logger.warning(
            "Invalid integer value supplied for env; using default",
            extra={"env_name": name, "env_value": raw, "default": default},
        )
        return default
    return value


_DEFAULT_NOTIFICATION_INTERVAL_HOURS = _parse_float_env("RENEWAL_NOTIFICATION_INTERVAL_HOURS", 24.0)
_DEFAULT_NOTIFICATION_RETRY_HOURS = _parse_float_env("RENEWAL_NOTIFICATION_RETRY_HOURS", 1.0)
_DEFAULT_BATCH_SIZE = max(1, _parse_int_env("RENEWAL_NOTIFICATION_BATCH_SIZE", 10))

_DEFAULT_SYSTEM_PROMPT = (
    "Ты — продуктовый маркетолог сервиса VPN_GPT. Пиши по-русски, дружелюбно и "
    "ненавязчиво, укладываясь максимум в три коротких абзаца без списков. Эмодзи "
    "используй лишь при явной пользе и обязательно заверши мягким призывом выбрать "
    "тариф или оплатить доступ."
)


@dataclass(slots=True)
class NotificationJob:
    id: int
    key_uuid: str
    chat_id: int | None
    username: str | None
    expires_at: str | None
    stage: int
    last_sent_at: str | None
    next_attempt_at: str | None
    last_error: str | None


class RenewalNotificationGenerator:
    def __init__(
        self,
        *,
        client: OpenAI | None = None,
        api_key: str | None = None,
        model: str | None = None,
        system_prompt: str | None = None,
        payment_url: str | None = None,
        star_settings: StarSettings | None = None,
    ) -> None:
        if client is None:
            resolved_key = api_key or os.getenv("RENEWAL_NOTIFICATION_GPT_API_KEY") or os.getenv("GPT_API_KEY")
            if not resolved_key:
                raise RuntimeError("GPT API key is required for renewal notifications")
            client = OpenAI(api_key=resolved_key)
        self._client = client
        self._model = model or os.getenv("RENEWAL_NOTIFICATION_MODEL") or os.getenv("GPT_MODEL", "gpt-4o-mini")
        self._system_prompt = system_prompt or os.getenv("RENEWAL_NOTIFICATION_SYSTEM_PROMPT") or _DEFAULT_SYSTEM_PROMPT
        raw_payment_url = payment_url or os.getenv("BOT_PAYMENT_URL") or "https://vpn-gpt.store/payment.html"
        self._payment_url = raw_payment_url.rstrip("/")
        self._star_settings = star_settings or config.STAR_SETTINGS

    def _plan_lookup(self) -> tuple[StarPlan | None, StarPlan | None, list[StarPlan]]:
        settings = self._star_settings
        test_plan = settings.plans.get("test_1d")
        month_plan = settings.plans.get("1m")
        excluded = {plan.code for plan in (test_plan, month_plan) if plan is not None}

        ordered_codes = [code for code in ("3m", "12m", "1y") if code not in excluded]
        extras: list[StarPlan] = []
        for code in ordered_codes:
            plan = settings.plans.get(code)
            if plan and plan not in extras:
                extras.append(plan)
        for plan in settings.available_plans():
            if plan.code in excluded:
                continue
            if plan in extras:
                continue
            extras.append(plan)
        return test_plan, month_plan, extras

    @staticmethod
    def _format_plan(plan: StarPlan) -> str:
        label = plan.label if plan.is_subscription else plan.title
        return f"{label} — {plan.price_stars}⭐"

    def build_prompt(self, stage: int, job: NotificationJob) -> list[dict[str, str]]:
        username = job.username or "друг"
        expires_at = job.expires_at or "уже закончилась"
        payment_url = self._payment_url
        test_plan, month_plan, extra_plans = self._plan_lookup()

        plan_lines: list[str] = []
        if test_plan:
            plan_lines.append(
                f"Тестовый доступ ({test_plan.title.lower()}) длится 24 часа и стоит {test_plan.price_stars}⭐."
            )
        if month_plan:
            plan_lines.append(
                f"Месячная подписка теперь стоит {month_plan.price_stars}⭐ — автопродление в Telegram."
            )
        if extra_plans:
            extras = ", ".join(self._format_plan(plan) for plan in extra_plans)
            plan_lines.append(f"Другие подписки без изменений: {extras}.")
        if not plan_lines:
            plan_lines.append("Доступ можно продлить через Telegram Stars — оплата занимает пару кликов.")

        facts = "\n".join(f"- {line}" for line in plan_lines)
        user_content = (
            "Сформируй продающее, но деликатное сообщение для клиента Telegram.\n"
            f"Имя клиента (если известно): {username}.\n"
            f"Доступ закончился: {expires_at}.\n"
            f"Оплата доступна по ссылке: {payment_url}.\n"
            "Ключевые факты:\n"
            f"{facts}\n"
            "- Оплата проходит через Telegram Stars в пару кликов — предложи удобный сценарий.\n"
            "- Подчеркни выгоды сервиса VPN_GPT и готовность помочь с настройкой или оплатой."
        )
        return [
            {"role": "system", "content": self._system_prompt},
            {"role": "user", "content": user_content},
        ]

    def generate(self, stage: int, job: NotificationJob) -> str:
        messages = self.build_prompt(stage, job)
        completion = self._client.chat.completions.create(model=self._model, messages=messages)
        text = completion.choices[0].message.content or ""
        return text.strip()


class RenewalNotificationScheduler:
    def __init__(
        self,
        *,
        interval_seconds: float = 300.0,
        batch_size: int = _DEFAULT_BATCH_SIZE,
        interval_hours: float = _DEFAULT_NOTIFICATION_INTERVAL_HOURS,
        retry_hours: float = _DEFAULT_NOTIFICATION_RETRY_HOURS,
        text_generator: RenewalNotificationGenerator | None = None,
        send_message: Callable[[int, str], Awaitable[Any] | Any] | None = None,
        fetch_jobs: Callable[[int | None], Sequence[dict[str, Any]]] | None = None,
        mark_sent: Callable[[int, bool], None] | None = None,
        mark_failed: Callable[[int, str], None] | None = None,
        complete_chain: Callable[[int], None] | None = None,
    ) -> None:
        if interval_seconds <= 0:
            logger.warning(
                "Invalid renewal notification poll interval; using default",
                extra={"interval_seconds": interval_seconds},
            )
            interval_seconds = 300.0
        self.interval_seconds = float(interval_seconds)
        self.batch_size = max(1, int(batch_size))
        self._interval_hours = interval_hours if interval_hours > 0 else _DEFAULT_NOTIFICATION_INTERVAL_HOURS
        self._retry_hours = retry_hours if retry_hours > 0 else _DEFAULT_NOTIFICATION_RETRY_HOURS
        self._text_generator = text_generator or RenewalNotificationGenerator()
        self._send_message = send_message or telegram_send_message
        self._fetch_jobs = fetch_jobs or (lambda limit: db.list_due_renewal_notifications(limit=limit))
        self._mark_sent = mark_sent or self._default_mark_sent
        self._mark_failed = mark_failed or self._default_mark_failed
        self._complete_chain = complete_chain or db.complete_renewal_notification
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def _default_mark_sent(self, notification_id: int, has_more: bool) -> None:
        db.mark_notification_sent(
            notification_id,
            has_more=has_more,
            interval_hours=self._interval_hours,
        )

    def _default_mark_failed(self, notification_id: int, error: str) -> None:
        db.mark_notification_failed(
            notification_id,
            error,
            retry_hours=self._retry_hours,
        )

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            logger.debug("Renewal notification scheduler already running")
            return
        self._stop_event.clear()
        thread = threading.Thread(target=self._run_loop, name="renewal-notifier", daemon=True)
        thread.start()
        self._thread = thread
        logger.info(
            "Renewal notification scheduler started",
            extra={"interval_seconds": self.interval_seconds, "batch_size": self.batch_size},
        )

    def stop(self) -> None:
        self._stop_event.set()
        thread = self._thread
        if thread and thread.is_alive():
            thread.join(timeout=self.interval_seconds + 1.0)
        self._thread = None
        logger.info("Renewal notification scheduler stopped")

    def run_once(self) -> int:
        try:
            raw_jobs = self._fetch_jobs(self.batch_size)
        except Exception:
            logger.exception("Failed to load pending renewal notifications")
            return 0

        jobs = [self._to_job(payload) for payload in raw_jobs]
        processed = 0

        for job in jobs:
            if job.chat_id is None:
                logger.info(
                    "Skipping renewal notification without chat_id",
                    extra={"uuid": job.key_uuid},
                )
                self._complete_chain(job.id)
                continue

            if job.stage >= db.RENEWAL_NOTIFICATION_STAGE_COUNT:
                self._complete_chain(job.id)
                continue

            try:
                message = self._text_generator.generate(job.stage + 1, job)
            except Exception as exc:
                logger.exception(
                    "Failed to generate renewal notification text",
                    extra={"uuid": job.key_uuid, "stage": job.stage + 1},
                )
                self._mark_failed(job.id, str(exc))
                continue

            if not message.strip():
                logger.warning(
                    "Generated empty renewal notification message",
                    extra={"uuid": job.key_uuid, "stage": job.stage + 1},
                )
                self._mark_failed(job.id, "empty message")
                continue

            try:
                self._dispatch_message(job.chat_id, message)
            except Exception as exc:
                logger.exception(
                    "Failed to deliver renewal notification",
                    extra={"uuid": job.key_uuid, "stage": job.stage + 1},
                )
                self._mark_failed(job.id, str(exc))
                continue

            has_more = job.stage + 1 < db.RENEWAL_NOTIFICATION_STAGE_COUNT
            self._mark_sent(job.id, has_more)
            processed += 1

        if processed or jobs:
            logger.info(
                "Renewal notification sweep completed",
                extra={"processed": processed, "candidates": len(jobs)},
            )

        return processed

    def _dispatch_message(self, chat_id: int, message: str) -> Any:
        result = self._send_message(chat_id, message)
        if asyncio.iscoroutine(result):
            return asyncio.run(result)
        return result

    def _to_job(self, payload: dict[str, Any]) -> NotificationJob:
        return NotificationJob(
            id=int(payload.get("id")),
            key_uuid=str(payload.get("key_uuid")),
            chat_id=payload.get("chat_id"),
            username=payload.get("username"),
            expires_at=payload.get("expires_at"),
            stage=int(payload.get("stage", 0) or 0),
            last_sent_at=payload.get("last_sent_at"),
            next_attempt_at=payload.get("next_attempt_at"),
            last_error=payload.get("last_error"),
        )

    def _run_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                self.run_once()
            except Exception:
                logger.exception("Unexpected error during renewal notification sweep")
            if self._stop_event.wait(self.interval_seconds):
                break


def schedule_notification_chain(record: dict[str, Any]) -> bool:
    """Schedule a renewal notification chain for an expired VPN key."""

    key_uuid = record.get("uuid")
    chat_id = record.get("chat_id")
    username = record.get("username")
    expires_at = record.get("expires_at")

    if not key_uuid:
        logger.warning("Cannot schedule renewal notification without UUID")
        return False

    try:
        scheduled = db.schedule_renewal_notification(
            key_uuid,
            chat_id=chat_id,
            username=username,
            expires_at=expires_at,
        )
    except Exception:
        logger.exception(
            "Failed to persist renewal notification chain",
            extra={"uuid": key_uuid, "chat_id": chat_id},
        )
        return False

    if scheduled:
        logger.info(
            "Queued renewal notification chain",
            extra={"uuid": key_uuid, "chat_id": chat_id},
        )
    else:
        logger.debug(
            "Renewal notification chain already queued",
            extra={"uuid": key_uuid, "chat_id": chat_id},
        )
    return scheduled


__all__ = [
    "NotificationJob",
    "RenewalNotificationGenerator",
    "RenewalNotificationScheduler",
    "schedule_notification_chain",
]
