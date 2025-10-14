import sqlite3

from api.utils import db


def test_run_with_schema_retry_handles_no_column_named_label(monkeypatch):
    calls = []

    def fake_migration(**_kwargs):
        calls.append(True)

    attempts = {"count": 0}

    def operation():
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise sqlite3.OperationalError("table vpn_keys has no column named label")
        return "ok"

    monkeypatch.setattr(db, "auto_update_missing_fields", fake_migration)

    result = db._run_with_schema_retry(operation)

    assert result == "ok"
    assert len(calls) == 1
    assert attempts["count"] == 2
