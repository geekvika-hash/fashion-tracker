"""
database.py — SQLite storage for tracked items
"""
import sqlite3
import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

DB_PATH = Path("trackings.db")


def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    """Create tables if they don't exist."""
    with get_conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS trackings (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id     INTEGER NOT NULL,
                url         TEXT    NOT NULL,
                product_name TEXT,
                size        TEXT    NOT NULL,
                active      INTEGER DEFAULT 1,
                notified    INTEGER DEFAULT 0,
                created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.commit()
    logger.info("Database ready.")


def add_tracking(chat_id: int, url: str, product_name: str, size: str) -> int:
    """Add a new item to track. Returns the new row id."""
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO trackings (chat_id, url, product_name, size) VALUES (?, ?, ?, ?)",
            (chat_id, url, product_name, size),
        )
        conn.commit()
        return cur.lastrowid


def get_active_trackings():
    """Return all active tracking rows."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM trackings WHERE active = 1"
        ).fetchall()
    return [dict(r) for r in rows]


def get_user_trackings(chat_id: int):
    """Return all active trackings for a specific user."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM trackings WHERE chat_id = ? AND active = 1 ORDER BY created_at DESC",
            (chat_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def stop_tracking(tracking_id: int):
    """Mark a tracking as inactive."""
    with get_conn() as conn:
        conn.execute(
            "UPDATE trackings SET active = 0 WHERE id = ?",
            (tracking_id,),
        )
        conn.commit()


def mark_notified(tracking_id: int):
    """Remember that we already sent a notification for this item (still active)."""
    with get_conn() as conn:
        conn.execute(
            "UPDATE trackings SET notified = 1 WHERE id = ?",
            (tracking_id,),
        )
        conn.commit()


def unmark_notified(tracking_id: int):
    """Reset notified flag (e.g. size went out of stock again)."""
    with get_conn() as conn:
        conn.execute(
            "UPDATE trackings SET notified = 0 WHERE id = ?",
            (tracking_id,),
        )
        conn.commit()
