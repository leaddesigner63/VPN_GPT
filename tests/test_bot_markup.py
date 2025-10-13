"""Tests for helper functions used to build bot inline keyboards."""

import os
import sys
from importlib import util
from pathlib import Path

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
