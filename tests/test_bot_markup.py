"""Tests for helper functions used to build bot inline keyboards."""

import os
import sys
from importlib import util
from pathlib import Path

import pytest
from aiogram.types import InlineKeyboardButton

os.environ.setdefault("BOT_TOKEN", "123456:TESTTOKEN")
os.environ.setdefault("GPT_API_KEY", "test")

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
_BOT_SPEC = util.spec_from_file_location("vpn_gpt_root_bot", _ROOT / "bot.py")
assert _BOT_SPEC and _BOT_SPEC.loader  # for type checkers
bot_module = util.module_from_spec(_BOT_SPEC)
_BOT_SPEC.loader.exec_module(bot_module)

_is_supported_button_link = bot_module._is_supported_button_link
build_result_markup = bot_module.build_result_markup
_format_active_key_quick_start_message = bot_module._format_active_key_quick_start_message
_should_offer_tariffs = bot_module._should_offer_tariffs
_format_active_subscription_notice = bot_module._format_active_subscription_notice


def test_is_supported_button_link_accepts_http_and_https():
    assert _is_supported_button_link("https://example.com")
    assert _is_supported_button_link("http://example.com/path")


def test_is_supported_button_link_accepts_telegram_deeplink():
    assert _is_supported_button_link("tg://resolve?domain=example")


def test_is_supported_button_link_rejects_other_schemes():
    assert not _is_supported_button_link("vless://uuid@host")
    assert not _is_supported_button_link("ftp://example.com")
    assert not _is_supported_button_link("mailto:user@example.com")


def test_build_result_markup_does_not_add_button_for_unsupported_links():
    markup = build_result_markup("vless://uuid@host")

    link_buttons = [
        button
        for row in markup.inline_keyboard
        for button in row
        if isinstance(button, InlineKeyboardButton) and button.url
    ]

    assert not link_buttons

    callback_data = {
        button.callback_data
        for row in markup.inline_keyboard
        for button in row
        if isinstance(button, InlineKeyboardButton) and button.callback_data
    }

    assert "show_qr" in callback_data


def test_build_result_markup_adds_button_for_supported_link():
    url = "https://vpn-gpt.store"
    markup = build_result_markup(url)

    link_buttons = [
        button
        for row in markup.inline_keyboard
        for button in row
        if isinstance(button, InlineKeyboardButton) and button.url
    ]

    assert link_buttons
    assert link_buttons[0].url == url


def test_build_result_markup_contains_action_buttons():
    markup = build_result_markup()

    callback_data = {
        button.callback_data
        for row in markup.inline_keyboard
        for button in row
        if isinstance(button, InlineKeyboardButton) and button.callback_data
    }

    expected_actions = {"menu_quick", "menu_keys", "menu_pay", "menu_help", "menu_back"}
    assert expected_actions.issubset(callback_data)


def test_format_active_key_quick_start_message_includes_subscription_notice():
    message = _format_active_key_quick_start_message([
        {
            "expires_at": "2024-06-01T12:00:00",
            "active": 1,
            "is_subscription": True,
        },
    ])

    assert "Текущий ключ действует до: 2024-06-01T12:00:00" in message
    assert "Подписка обновляется автоматически" in message


def test_format_active_key_quick_start_message_prompts_extension_without_subscription():
    message = _format_active_key_quick_start_message([
        {
            "expires_at": "2024-07-10T08:00:00",
            "active": 1,
            "trial": False,
            "is_subscription": False,
        },
    ])

    assert "Текущий ключ действует до: 2024-07-10T08:00:00" in message
    assert "Продли доступ" in message
    assert "Подписка обновляется автоматически" not in message


def test_format_active_key_quick_start_message_handles_missing_expiry():
    message = _format_active_key_quick_start_message([
        {"expires_at": None, "active": 1, "is_subscription": False},
    ])

    assert "Текущий ключ действует до: —" in message
    assert "🔑 Мои ключи" in message


def test_format_active_key_quick_start_message_requires_active_keys():
    with pytest.raises(ValueError):
        _format_active_key_quick_start_message([])


def test_should_offer_tariffs_permits_trial_access():
    keys = [
        {"active": 1, "trial": True, "is_subscription": False},
        {"active": 0, "trial": False, "is_subscription": False},
    ]

    assert _should_offer_tariffs(keys)


def test_should_offer_tariffs_blocks_active_subscription():
    keys = [
        {"active": 1, "trial": False, "is_subscription": True},
        {"active": 1, "trial": True, "is_subscription": False},
    ]

    assert not _should_offer_tariffs(keys)


def test_format_active_subscription_notice_mentions_expiry():
    message = _format_active_subscription_notice(
        {"expires_at": "2024-10-01T00:00:00", "label": "1 месяц"}
    )

    assert "Подписка уже активна" in message
    assert "2024-10-01T00:00:00" in message
