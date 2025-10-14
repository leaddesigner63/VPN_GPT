from __future__ import annotations

import importlib
from datetime import datetime


def _prepare_modules(tmp_path, monkeypatch):
    db_path = tmp_path / "notify.db"
    monkeypatch.setenv("DATABASE", str(db_path))

    import api.utils.db as db_module

    importlib.reload(db_module)
    db_module.init_db()

    import api.utils.notifications as notifications_module

    importlib.reload(notifications_module)
    return db_module, notifications_module


def _force_due(db_module, key_uuid: str) -> None:
    with db_module.connect() as con:
        con.execute(
            "UPDATE renewal_notifications SET next_attempt_at=? WHERE key_uuid=?",
            ("1970-01-01T00:00:00", key_uuid),
        )


def test_scheduler_sends_three_notifications(tmp_path, monkeypatch):
    db_module, notifications = _prepare_modules(tmp_path, monkeypatch)

    db_module.schedule_renewal_notification(
        "uuid-123", chat_id=555, username="alice", expires_at=datetime.utcnow().isoformat()
    )

    calls: list[tuple[int, str]] = []

    class StubGenerator:
        def generate(self, stage: int, job):
            calls.append((stage, job.key_uuid))
            return f"message-{stage}"

    sent_messages: list[tuple[int, str]] = []

    async def fake_send(chat_id: int, text: str):
        sent_messages.append((chat_id, text))

    scheduler = notifications.RenewalNotificationScheduler(
        interval_seconds=0.01,
        text_generator=StubGenerator(),
        send_message=fake_send,
    )

    assert scheduler.run_once() == 1

    record = db_module.get_renewal_notification("uuid-123")
    assert record is not None
    assert record["stage"] == 1
    assert record["completed"] == 0
    assert sent_messages == [(555, "message-1")]

    _force_due(db_module, "uuid-123")
    assert scheduler.run_once() == 1

    record = db_module.get_renewal_notification("uuid-123")
    assert record["stage"] == 2
    assert record["completed"] == 0

    _force_due(db_module, "uuid-123")
    assert scheduler.run_once() == 1

    record = db_module.get_renewal_notification("uuid-123")
    assert record["stage"] == 3
    assert record["completed"] == 1

    assert calls == [(1, "uuid-123"), (2, "uuid-123"), (3, "uuid-123")]
    assert sent_messages == [
        (555, "message-1"),
        (555, "message-2"),
        (555, "message-3"),
    ]

    _force_due(db_module, "uuid-123")
    assert scheduler.run_once() == 0


def test_scheduler_recovers_from_generation_error(tmp_path, monkeypatch):
    db_module, notifications = _prepare_modules(tmp_path, monkeypatch)

    db_module.schedule_renewal_notification(
        "uuid-err", chat_id=777, username="bob", expires_at=datetime.utcnow().isoformat()
    )

    class FailingGenerator:
        def generate(self, stage: int, job):
            raise RuntimeError("failed to craft text")

    async def fake_send(chat_id: int, text: str):  # pragma: no cover - not expected
        raise AssertionError("send should not be called when generation fails")

    scheduler = notifications.RenewalNotificationScheduler(
        interval_seconds=0.01,
        text_generator=FailingGenerator(),
        send_message=fake_send,
        retry_hours=0.01,
    )

    assert scheduler.run_once() == 0

    record = db_module.get_renewal_notification("uuid-err")
    assert record is not None
    assert record["stage"] == 0
    assert record["completed"] == 0
    assert "failed to craft text" in (record["last_error"] or "")

    next_attempt = datetime.fromisoformat(record["next_attempt_at"])
    assert next_attempt > datetime.utcnow()
