"""Ensure the payments table exposes Morune order identifiers."""
from __future__ import annotations

import sqlite3

__all__ = ["ensure_order_id_column"]


def ensure_order_id_column(con: sqlite3.Connection) -> None:
    """Add the ``order_id`` column if it is missing and index it."""

    cur = con.execute("PRAGMA table_info(payments)")
    columns = {row[1] for row in cur.fetchall()}
    if "order_id" not in columns:
        con.execute("ALTER TABLE payments ADD COLUMN order_id TEXT")
        con.execute(
            "UPDATE payments SET order_id=payment_id WHERE order_id IS NULL OR order_id=''"
        )
    con.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_payments_order_id ON payments(order_id)")
