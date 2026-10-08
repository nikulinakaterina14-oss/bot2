import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Optional

# On bothost.ru the persistent Volume (Basic/Pro plans) is mounted at /app/data,
# so the DB file must live there to survive container restarts/redeploys.
# Falls back to the project folder for local runs where DATA_DIR isn't set.
_data_dir = os.getenv("DATA_DIR")
if _data_dir:
    Path(_data_dir).mkdir(parents=True, exist_ok=True)
    DB_PATH = Path(_data_dir) / "events.db"
else:
    DB_PATH = Path(__file__).parent / "events.db"


@contextmanager
def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    with get_conn() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                username TEXT PRIMARY KEY,
                chat_id INTEGER NOT NULL,
                first_seen TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                description TEXT,
                event_time TEXT NOT NULL,
                creator_chat_id INTEGER NOT NULL,
                creator_username TEXT,
                reminder_48_sent INTEGER NOT NULL DEFAULT 0,
                reminder_24_sent INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS event_subscribers (
                event_id INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
                username TEXT NOT NULL,
                PRIMARY KEY (event_id, username)
            )
            """
        )


def upsert_user(username: Optional[str], chat_id: int) -> None:
    if not username:
        return
    username = username.lower()
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO users (username, chat_id, first_seen)
            VALUES (?, ?, ?)
            ON CONFLICT(username) DO UPDATE SET chat_id = excluded.chat_id
            """,
            (username, chat_id, datetime.utcnow().isoformat()),
        )


def get_chat_id_for_username(username: str) -> Optional[int]:
    username = username.lower().lstrip("@")
    with get_conn() as conn:
        row = conn.execute(
            "SELECT chat_id FROM users WHERE username = ?", (username,)
        ).fetchone()
        return row["chat_id"] if row else None


def create_event(
    name: str,
    description: str,
    event_time_iso: str,
    creator_chat_id: int,
    creator_username: Optional[str],
    subscriber_usernames: list[str],
) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            """
            INSERT INTO events (name, description, event_time, creator_chat_id,
                                 creator_username, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                name,
                description,
                event_time_iso,
                creator_chat_id,
                (creator_username or "").lower() or None,
                datetime.utcnow().isoformat(),
            ),
        )
        event_id = cur.lastrowid
        for uname in subscriber_usernames:
            uname = uname.lower().lstrip("@")
            if not uname:
                continue
            conn.execute(
                "INSERT OR IGNORE INTO event_subscribers (event_id, username) VALUES (?, ?)",
                (event_id, uname),
            )
        return event_id


def get_event(event_id: int) -> Optional[sqlite3.Row]:
    with get_conn() as conn:
        return conn.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()


def get_subscribers(event_id: int) -> list[str]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT username FROM event_subscribers WHERE event_id = ?", (event_id,)
        ).fetchall()
        return [r["username"] for r in rows]


def get_future_events() -> list[sqlite3.Row]:
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM events WHERE event_time >= ? ORDER BY event_time ASC",
            (datetime.utcnow().isoformat(),),
        ).fetchall()


def get_events_for_user(username: Optional[str], chat_id: int) -> list[sqlite3.Row]:
    username = (username or "").lower()
    with get_conn() as conn:
        return conn.execute(
            """
            SELECT DISTINCT e.* FROM events e
            LEFT JOIN event_subscribers s ON s.event_id = e.id
            WHERE e.event_time >= ?
              AND (e.creator_chat_id = ? OR s.username = ?)
            ORDER BY e.event_time ASC
            """,
            (datetime.utcnow().isoformat(), chat_id, username),
        ).fetchall()


def get_events_created_by(chat_id: int) -> list[sqlite3.Row]:
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM events WHERE creator_chat_id = ? ORDER BY event_time ASC",
            (chat_id,),
        ).fetchall()


def delete_event(event_id: int) -> None:
    with get_conn() as conn:
        conn.execute("DELETE FROM event_subscribers WHERE event_id = ?", (event_id,))
        conn.execute("DELETE FROM events WHERE id = ?", (event_id,))


def mark_reminder_sent(event_id: int, stage: str) -> None:
    column = "reminder_48_sent" if stage == "48h" else "reminder_24_sent"
    with get_conn() as conn:
        conn.execute(f"UPDATE events SET {column} = 1 WHERE id = ?", (event_id,))
