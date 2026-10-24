import sqlite3
import os
from contextlib import contextmanager
from datetime import datetime

DATA_DIR = os.getenv("DATA_DIR", "./data")
try:
    os.makedirs(DATA_DIR, exist_ok=True)
    _test_p = os.path.join(DATA_DIR, ".write_test")
    with open(_test_p, "w") as f:
        f.write("1")
    os.remove(_test_p)
except Exception:
    DATA_DIR = "/tmp"
    os.makedirs(DATA_DIR, exist_ok=True)

DB_PATH = os.path.join(DATA_DIR, "okulai.db")

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

@contextmanager
def db_session():
    conn = get_db()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

def init_db():
    with db_session() as conn:
        cursor = conn.cursor()
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS profiles (
            id TEXT PRIMARY KEY,
            name TEXT,
            grade INTEGER DEFAULT 9,
            created_at TEXT
        )
        """)
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS mistakes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            profile_id TEXT,
            grade INTEGER,
            subject TEXT,
            question_summary TEXT,
            student_error TEXT,
            ai_advice TEXT,
            created_at TEXT
        )
        """)
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS daily_quotas (
            tracking_key TEXT PRIMARY KEY,
            request_count INTEGER DEFAULT 0,
            last_date TEXT
        )
        """)
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS book_pages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            grade INTEGER,
            subject TEXT,
            book_name TEXT,
            page_num INTEGER,
            content TEXT
        )
        """)
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            val TEXT
        )
        """)

init_db()
