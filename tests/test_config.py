from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def reload_config(monkeypatch, **env):
    for key in ("BOT_TOKEN", "GPT_API_KEY", "GPT_ASSISTANT_ID", "ADMIN_ID"):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)

    if "config" in sys.modules:
        del sys.modules["config"]
    return importlib.import_module("config")


def test_admin_id_cast(monkeypatch):
    module = reload_config(monkeypatch, ADMIN_ID="101")
    assert module.ADMIN_ID == 101


def test_admin_id_invalid_value(monkeypatch):
    with pytest.raises(RuntimeError):
        reload_config(monkeypatch, ADMIN_ID="not-an-int")


def test_optional_values_default_to_none(monkeypatch):
    module = reload_config(monkeypatch)
    assert module.BOT_TOKEN is None
    assert module.GPT_API_KEY is None
    assert module.GPT_ASSISTANT_ID is None
    assert module.ADMIN_ID is None
