from __future__ import annotations

import json
import os
from datetime import datetime, timedelta
from pathlib import Path

import importlib


def test_expired_key_monitor_deactivates_and_syncs(tmp_path, monkeypatch):
    db_path = tmp_path / "expired.db"
    monkeypatch.setenv("DATABASE", str(db_path))

    import api.utils.db as db_module

    importlib.reload(db_module)
    db_module.init_db()

    now = datetime.utcnow()
    expired_uuid = "expired-key"
    active_uuid = "active-key"

    db_module.create_vpn_key(
        username="alice",
        chat_id=101,
        uuid_value=expired_uuid,
        link="vless://expired",
        expires_at=now - timedelta(hours=1),
        label="alice@example.com",
    )

    db_module.create_vpn_key(
        username="bob",
        chat_id=202,
        uuid_value=active_uuid,
        link="vless://active",
        expires_at=now + timedelta(days=1),
        label="bob@example.com",
    )

    from api.utils.expired_keys import ExpiredKeyMonitor

    monitor = ExpiredKeyMonitor(interval_seconds=0.01)

    assert db_module.list_expired_keys()

    processed = monitor.run_once()
    assert processed == 1

    expired_record = db_module.get_key_by_uuid(expired_uuid)
    active_record = db_module.get_key_by_uuid(active_uuid)

    assert expired_record is not None
    assert expired_record["active"] == 0

    assert active_record is not None
    assert active_record["active"] == 1

    config_path = Path(os.environ["XRAY_CONFIG"])
    config = json.loads(config_path.read_text(encoding="utf-8"))
    clients = config["inbounds"][0]["settings"]["clients"]

    assert all(client.get("id") != expired_uuid for client in clients)
    assert any(client.get("id") == active_uuid for client in clients)

    assert monitor.run_once() == 0
