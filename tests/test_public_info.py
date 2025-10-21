from __future__ import annotations

from pathlib import Path
import sys

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from utils.public_info import (
    build_public_info_prompt,
    build_vless_client_guard_prompt,
    get_public_info_prompt,
    load_public_info,
)


def test_load_public_info_returns_mapping(tmp_path: Path) -> None:
    sample = tmp_path / "public.json"
    sample.write_text('{"name": "VPN_GPT", "website": "https://vpn-gpt.store"}', encoding="utf-8")

    data = load_public_info(sample)

    assert data["name"] == "VPN_GPT"
    assert data["website"] == "https://vpn-gpt.store"


def test_load_public_info_missing_file(tmp_path: Path) -> None:
    missing = tmp_path / "absent.json"
    assert load_public_info(missing) == {}


def test_build_public_info_prompt_contains_core_fields() -> None:
    payload = {
        "name": "VPN_GPT",
        "website": "https://vpn-gpt.store",
        "telegram_bot": "https://t.me/dobriyvpn_bot",
        "contacts": [
            {"label": "Создатель", "value": "@ai_leaddesigner", "url": "https://t.me/ai_leaddesigner"}
        ],
        "pricing": [
            {"plan": "1 месяц", "price": "80⭐"},
        ],
    }

    prompt = build_public_info_prompt(payload)

    assert "https://vpn-gpt.store" in prompt
    assert "https://t.me/dobriyvpn_bot" in prompt
    assert "@ai_leaddesigner" in prompt
    assert "1 месяц" in prompt and "80⭐" in prompt


def test_build_public_info_prompt_empty_payload() -> None:
    assert build_public_info_prompt({}) == ""


def test_guard_prompt_only_allows_listed_clients() -> None:
    html = "• Android — <a href=\"https://example.com\">Example</a>\n• iOS — <a href=\"https://example.org\">Example</a>"
    prompt = build_vless_client_guard_prompt(html)

    assert "Example" in prompt
    assert "https://example.com" in prompt
    assert "не придумывай" in prompt.lower()


def test_get_public_info_prompt_uses_default_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    custom = tmp_path / "info.json"
    custom.write_text('{"description": "Test", "website": "https://vpn-gpt.store"}', encoding="utf-8")

    from utils import public_info as module

    monkeypatch.setattr(module, "_DEFAULT_PUBLIC_INFO_PATH", custom)

    prompt = get_public_info_prompt()

    assert "https://vpn-gpt.store" in prompt
    assert "Test" in prompt
