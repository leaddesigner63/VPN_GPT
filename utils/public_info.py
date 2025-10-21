from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any, Iterable


logger = logging.getLogger("vpn_gpt.public_info")

_DEFAULT_PUBLIC_INFO_PATH = Path(__file__).resolve().parent.parent / "public_info.json"


def load_public_info(path: Path | None = None) -> dict[str, Any]:
    """Load structured public information about the project.

    The data lives in a small JSON file that can be edited without touching the
    Python code. When the file is missing or invalid we fail gracefully and
    return an empty mapping.
    """

    info_path = Path(path) if path else _DEFAULT_PUBLIC_INFO_PATH
    try:
        raw = info_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        logger.warning(
            "Public info file is missing",
            extra={"path": str(info_path)},
        )
        return {}

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning(
            "Failed to parse public info JSON",
            extra={"path": str(info_path)},
        )
        return {}

    if not isinstance(data, dict):
        logger.warning(
            "Public info JSON must be an object",
            extra={"path": str(info_path)},
        )
        return {}

    return data


def _format_list_section(title: str, items: Iterable[str]) -> list[str]:
    lines: list[str] = []
    cleaned = [item.strip() for item in items if item and item.strip()]
    if not cleaned:
        return lines
    lines.append(title)
    for item in cleaned:
        lines.append(f"• {item}")
    return lines


def build_public_info_prompt(info: dict[str, Any]) -> str:
    """Transform public info JSON into a compact system instruction."""

    if not info:
        return ""

    name = str(info.get("name") or "VPN_GPT").strip()
    tagline = str(info.get("tagline") or "").strip()
    description = str(info.get("description") or "").strip()
    website = str(info.get("website") or "").strip()
    telegram_bot = str(info.get("telegram_bot") or "").strip()

    lines: list[str] = [
        f"Всегда отвечай на русском языке и веди себя как внимательный консультант проекта {name}.",
        (
            "Если собеседник просит актуальную публичную информацию о проекте, "
            "опирайся на проверенные факты ниже и обязательно включай ссылку на сайт."
        ),
    ]

    if description:
        lines.append(description)
    elif tagline:
        lines.append(tagline)

    if website:
        lines.append(f"Официальный сайт: {website}")
    if telegram_bot:
        lines.append(f"Телеграм-бот: {telegram_bot}")

    creator = str(info.get("creator") or "").strip()
    if creator:
        lines.append(f"Создатель проекта: {creator}")

    contacts = info.get("contacts")
    contact_lines: list[str] = []
    if isinstance(contacts, list):
        for contact in contacts:
            if not isinstance(contact, dict):
                continue
            label = str(contact.get("label") or "").strip()
            value = str(contact.get("value") or "").strip()
            url = str(contact.get("url") or "").strip()
            if not (label and value):
                continue
            if url:
                contact_lines.append(f"{label}: {value} ({url})")
            else:
                contact_lines.append(f"{label}: {value}")
    lines.extend(_format_list_section("Контакты:", contact_lines))

    highlights = info.get("highlights")
    if isinstance(highlights, list):
        lines.extend(
            _format_list_section(
                "Ключевые факты о сервисе:",
                [str(item) for item in highlights],
            )
        )

    pricing = info.get("pricing")
    pricing_lines: list[str] = []
    if isinstance(pricing, list):
        for item in pricing:
            if not isinstance(item, dict):
                continue
            plan = str(item.get("plan") or "").strip()
            price = str(item.get("price") or "").strip()
            note = str(item.get("note") or "").strip()
            if not (plan and price):
                continue
            if note:
                pricing_lines.append(f"{plan} — {price} ({note})")
            else:
                pricing_lines.append(f"{plan} — {price}")
    lines.extend(_format_list_section("Текущие тарифы:", pricing_lines))

    return "\n".join(lines)


def build_vless_client_guard_prompt(recommendations_html: str) -> str:
    """Create a guardrail prompt for VLESS client recommendations."""

    snippet = (recommendations_html or "").strip()
    if not snippet:
        return ""

    text_only = re.sub(r"<[^>]+>", "", snippet)
    normalized = re.sub(r"\s+", " ", text_only).strip()
    return (
        "Не придумывай новые VPN-клиенты. Рекомендуй приложения только из официального списка "
        "для протокола VLESS и сохраняй HTML-формат «• ОС — <a href=\"URL\">Название</a>».\n"
        f"Разрешённые варианты: {normalized}.\n"
        "Используй следующий фрагмент как единственный источник:") + f"\n{snippet}\n" + (
        "Если пользователь просит альтернативы, объясни, что пока доступны только перечисленные клиенты."
    )


def get_public_info_prompt(path: Path | None = None) -> str:
    """Helper that loads JSON and returns a ready-to-use prompt."""

    info = load_public_info(path)
    return build_public_info_prompt(info)

