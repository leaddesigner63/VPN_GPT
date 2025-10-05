import sqlite3
from datetime import datetime, timedelta

DB_PATH = "/root/VPN_GPT/dialogs.db"

def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    # История общения
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

    # Учёт VPN-ключей
    c.execute("""
        CREATE TABLE IF NOT EXISTS vpn_keys (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            username TEXT,
            full_name TEXT,
            key_uuid TEXT,
            link TEXT,
            issued_at TEXT,
            expires_at TEXT,
            active INTEGER DEFAULT 1
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


def save_vpn_key(user_id, username, full_name, link, expires_at):
    import uuid
    key_uuid = str(uuid.uuid4())
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        INSERT INTO vpn_keys (user_id, username, full_name, key_uuid, link, issued_at, expires_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (user_id, username, full_name, key_uuid, link, datetime.utcnow().isoformat(), expires_at.isoformat()))
    conn.commit()
    conn.close()


def get_expiring_keys(days_before=3):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    threshold = (datetime.utcnow() + timedelta(days=days_before)).isoformat()
    c.execute("""
        SELECT user_id, full_name, expires_at FROM vpn_keys
        WHERE active = 1 AND expires_at <= ?
    """, (threshold,))
    rows = c.fetchall()
    conn.close()
def renew_vpn_key(user_id, extend_days=30):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    # Найдём последний активный ключ
    c.execute("""
        SELECT id, expires_at FROM vpn_keys
        WHERE user_id = ? AND active = 1
        ORDER BY id DESC LIMIT 1
    """, (user_id,))
    row = c.fetchone()

    if not row:
        conn.close()
        return None

    key_id, old_exp = row
    old_exp_date = datetime.fromisoformat(old_exp)
    new_exp_date = old_exp_date + timedelta(days=extend_days)

    # Обновляем срок действия
    c.execute("UPDATE vpn_keys SET expires_at = ? WHERE id = ?", (new_exp_date.isoformat(), key_id))
    conn.commit()
    conn.close()
    return new_exp_date

    # Преобразуем дату в datetime
    parsed = []
    for user_id, name, exp in rows:
        try:
            parsed.append((user_id, name, datetime.fromisoformat(exp)))
        except:
            continue
    return parsed

def get_expired_keys():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    now = datetime.utcnow().isoformat()
    c.execute("SELECT user_id, full_name, link FROM vpn_keys WHERE active = 1 AND expires_at < ?", (now,))
    rows = c.fetchall()
    conn.close()
    return rows


def deactivate_vpn_key(user_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("UPDATE vpn_keys SET active = 0 WHERE user_id = ?", (user_id,))
    conn.commit()
    conn.close()

def get_all_active_users():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT user_id, full_name, expires_at FROM vpn_keys WHERE active = 1 ORDER BY expires_at DESC")
    rows = c.fetchall()
    conn.close()
    return rows

