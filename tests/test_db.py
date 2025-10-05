from __future__ import annotations

import sqlite3
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from utils import db


@pytest.fixture()
def isolated_db(tmp_path, monkeypatch):
    database_path = tmp_path / "test.db"
    monkeypatch.setattr(db, "DB_PATH", str(database_path))
    db.init_db()
    return database_path


def test_save_and_get_last_messages(isolated_db):
    user_id = 123
    db.save_message(user_id, "user", "Test User", "Hello", "Hi there")
    db.save_message(user_id, "user", "Test User", "How are you?", "Great!")

    history = db.get_last_messages(user_id, limit=5)

    assert history == [
        ("Hello", "Hi there"),
        ("How are you?", "Great!"),
    ]


def test_vpn_key_lifecycle(isolated_db):
    user_id = 77
    expires_at = datetime.now(UTC) + timedelta(days=2)
    key_uuid = db.save_vpn_key(user_id, "vpn_user", "VPN User", "vless://example", expires_at)

    assert isinstance(key_uuid, str)
    assert len(key_uuid) > 0

    active_users = db.get_all_active_users()
    assert active_users == [(user_id, "VPN User", expires_at.isoformat())]

    renewed = db.renew_vpn_key(user_id, extend_days=5)
    assert renewed is not None
    assert renewed > expires_at

    soon_expiring = db.get_expiring_keys(days_before=10)
    assert (user_id, "VPN User", renewed) in soon_expiring

    # Force expiration
    expired_at = datetime.now(UTC) - timedelta(days=1)
    with sqlite3.connect(db.DB_PATH) as conn:
        conn.execute("UPDATE vpn_keys SET expires_at = ? WHERE user_id = ?", (expired_at.isoformat(), user_id))
        conn.commit()

    expired = db.get_expired_keys()
    assert expired == [(user_id, "VPN User", "vless://example")]

    db.deactivate_vpn_key(user_id)
    assert db.get_all_active_users() == []
