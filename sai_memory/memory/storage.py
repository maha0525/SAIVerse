from __future__ import annotations

import json
import os
import sqlite3
import time
import uuid
from dataclasses import dataclass
from typing import Iterable, List, Optional, Tuple

from sai_memory.logging_utils import debug


def _ensure_dir(path: str) -> None:
    d = os.path.dirname(path)
    if d and not os.path.exists(d):
        os.makedirs(d, exist_ok=True)


def init_db(db_path: str, *, check_same_thread: bool = True) -> sqlite3.Connection:
    _ensure_dir(db_path)
    conn = sqlite3.connect(db_path, check_same_thread=check_same_thread)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS threads (
          id TEXT PRIMARY KEY,
          resource_id TEXT,
          overview TEXT,
          overview_updated_at INTEGER
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS messages (
          id TEXT PRIMARY KEY,
          thread_id TEXT,
          role TEXT,
          content TEXT,
          resource_id TEXT,
          created_at INTEGER
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS embeddings (
          message_id TEXT PRIMARY KEY,
          vector TEXT
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_messages_thread_created ON messages(thread_id, created_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_messages_resource_created ON messages(resource_id, created_at)")
    conn.commit()
    return conn


def get_or_create_thread(conn: sqlite3.Connection, thread_id: str, resource_id: Optional[str] = None) -> None:
    cur = conn.execute("SELECT id FROM threads WHERE id=?", (thread_id,))
    if cur.fetchone() is None:
        conn.execute(
            "INSERT INTO threads(id, resource_id, overview, overview_updated_at) VALUES (?, ?, ?, ?)",
            (thread_id, resource_id, None, None),
        )
        conn.commit()


def set_thread_overview(conn: sqlite3.Connection, thread_id: str, overview: str) -> None:
    conn.execute(
        "UPDATE threads SET overview=?, overview_updated_at=? WHERE id=?",
        (overview, int(time.time()), thread_id),
    )
    conn.commit()


def get_thread_overview(conn: sqlite3.Connection, thread_id: str) -> Optional[str]:
    cur = conn.execute("SELECT overview FROM threads WHERE id=?", (thread_id,))
    row = cur.fetchone()
    return row[0] if row and row[0] is not None else None


@dataclass
class Message:
    id: str
    thread_id: str
    role: str
    content: str
    resource_id: Optional[str]
    created_at: int


def add_message(
    conn: sqlite3.Connection,
    thread_id: str,
    role: str,
    content: str,
    resource_id: Optional[str] = None,
    created_at: Optional[int] = None,
) -> str:
    mid = str(uuid.uuid4())
    ts = int(time.time()) if created_at is None else int(created_at)
    conn.execute(
        "INSERT INTO messages(id, thread_id, role, content, resource_id, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (mid, thread_id, role, content, resource_id, ts),
    )
    conn.commit()
    return mid


def upsert_embedding(conn: sqlite3.Connection, message_id: str, vector: Iterable[float]) -> None:
    v = json.dumps(list(vector))
    conn.execute(
        "INSERT INTO embeddings(message_id, vector) VALUES(?, ?) ON CONFLICT(message_id) DO UPDATE SET vector=excluded.vector",
        (message_id, v),
    )
    conn.commit()


def get_message(conn: sqlite3.Connection, message_id: str) -> Optional[Message]:
    cur = conn.execute(
        "SELECT id, thread_id, role, content, resource_id, created_at FROM messages WHERE id=?",
        (message_id,),
    )
    row = cur.fetchone()
    if not row:
        return None
    return Message(*row)


def get_messages_last(conn: sqlite3.Connection, thread_id: str, limit: int) -> List[Message]:
    cur = conn.execute(
        "SELECT id, thread_id, role, content, resource_id, created_at FROM messages WHERE thread_id=? ORDER BY created_at DESC LIMIT ?",
        (thread_id, limit),
    )
    rows = cur.fetchall()
    return [Message(*row) for row in rows][::-1]


def get_messages_paginated(conn: sqlite3.Connection, thread_id: str, page: int, page_size: int) -> List[Message]:
    offset = max(0, page) * max(1, page_size)
    cur = conn.execute(
        "SELECT id, thread_id, role, content, resource_id, created_at FROM messages WHERE thread_id=? ORDER BY created_at ASC LIMIT ? OFFSET ?",
        (thread_id, page_size, offset),
    )
    return [Message(*row) for row in cur.fetchall()]


def get_messages_by_resource(conn: sqlite3.Connection, resource_id: str) -> List[Message]:
    cur = conn.execute(
        "SELECT id, thread_id, role, content, resource_id, created_at FROM messages WHERE resource_id=? ORDER BY created_at ASC",
        (resource_id,),
    )
    return [Message(*row) for row in cur.fetchall()]


def get_embeddings_for_scope(
    conn: sqlite3.Connection,
    thread_id: Optional[str] = None,
    resource_id: Optional[str] = None,
) -> List[Tuple[Message, List[float]]]:
    if thread_id:
        cur = conn.execute(
            """
            SELECT m.id, m.thread_id, m.role, m.content, m.resource_id, m.created_at, e.vector
            FROM messages m JOIN embeddings e ON m.id = e.message_id
            WHERE m.thread_id=?
            ORDER BY m.created_at ASC
            """,
            (thread_id,),
        )
    elif resource_id:
        cur = conn.execute(
            """
            SELECT m.id, m.thread_id, m.role, m.content, m.resource_id, m.created_at, e.vector
            FROM messages m JOIN embeddings e ON m.id = e.message_id
            WHERE m.resource_id=?
            ORDER BY m.created_at ASC
            """,
            (resource_id,),
        )
    else:
        cur = conn.execute(
            """
            SELECT m.id, m.thread_id, m.role, m.content, m.resource_id, m.created_at, e.vector
            FROM messages m JOIN embeddings e ON m.id = e.message_id
            ORDER BY m.created_at ASC
            """
        )

    rows = cur.fetchall()
    out: List[Tuple[Message, List[float]]] = []
    for row in rows:
        msg = Message(*row[:6])
        vec = json.loads(row[6])
        out.append((msg, vec))
    return out


def get_messages_around(
    conn: sqlite3.Connection, thread_id: str, created_at: int, before: int, after: int
) -> List[Message]:
    cur = conn.execute(
        "SELECT id, thread_id, role, content, resource_id, created_at FROM messages WHERE thread_id=? AND created_at < ? ORDER BY created_at DESC LIMIT ?",
        (thread_id, created_at, before),
    )
    before_rows = [Message(*row) for row in cur.fetchall()][::-1]
    cur = conn.execute(
        "SELECT id, thread_id, role, content, resource_id, created_at FROM messages WHERE thread_id=? AND created_at > ? ORDER BY created_at ASC LIMIT ?",
        (thread_id, created_at, after),
    )
    after_rows = [Message(*row) for row in cur.fetchall()]
    return before_rows + after_rows


def count_threads(conn: sqlite3.Connection) -> int:
    cur = conn.execute("SELECT COUNT(*) FROM threads")
    return int(cur.fetchone()[0])


def sample_messages(conn: sqlite3.Connection, limit: int = 5) -> List[Message]:
    cur = conn.execute(
        "SELECT id, thread_id, role, content, resource_id, created_at FROM messages ORDER BY created_at DESC LIMIT ?",
        (limit,),
    )
    return [Message(*row) for row in cur.fetchall()]


def list_thread_ids(conn: sqlite3.Connection) -> List[str]:
    cur = conn.execute("SELECT id FROM threads ORDER BY id ASC")
    return [r[0] for r in cur.fetchall()]


def count_messages(conn: sqlite3.Connection) -> int:
    cur = conn.execute("SELECT COUNT(*) FROM messages")
    return int(cur.fetchone()[0])


def count_embeddings(conn: sqlite3.Connection) -> int:
    cur = conn.execute("SELECT COUNT(*) FROM embeddings")
    return int(cur.fetchone()[0])
