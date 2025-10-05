import sqlite3
from datetime import datetime

DB_PATH = "/opt/vpn-bot/dialogs.db"

def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            username TEXT,
            full_name TEXT,
            message TEXT,
            reply TEXT,
            created_at TEXT
        )
    """)
    conn.commit()
    conn.close()

def save_message(user_id, username, full_name, message, reply):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        INSERT INTO history (user_id, username, full_name, message, reply, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (user_id, username, full_name, message, reply, datetime.utcnow().isoformat()))
    conn.commit()
    conn.close()

def get_last_messages(user_id, limit=5):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        SELECT message, reply FROM history
        WHERE user_id = ?
        ORDER BY id DESC
        LIMIT ?
    """, (user_id, limit))
    rows = c.fetchall()
    conn.close()
    return rows[::-1]

