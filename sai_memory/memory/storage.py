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
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS message_embeddings (
          message_id TEXT,
          chunk_index INTEGER,
          vector TEXT,
          PRIMARY KEY (message_id, chunk_index)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_message_embeddings_msg ON message_embeddings(message_id)"
    )
    # Backfill legacy embeddings table into message_embeddings as chunk 0 when needed.
    conn.execute(
        """
        INSERT OR IGNORE INTO message_embeddings(message_id, chunk_index, vector)
        SELECT message_id, 0, vector FROM embeddings
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


def replace_message_embeddings(
    conn: sqlite3.Connection,
    message_id: str,
    vectors: Iterable[Iterable[float]],
) -> None:
    conn.execute("DELETE FROM message_embeddings WHERE message_id=?", (message_id,))
    payload: List[Tuple[str, int, str]] = []
    for idx, vec in enumerate(vectors):
        payload.append((message_id, idx, json.dumps(list(map(float, vec)))))
    if payload:
        conn.executemany(
            "INSERT INTO message_embeddings(message_id, chunk_index, vector) VALUES(?, ?, ?)",
            payload,
        )
    conn.commit()


def upsert_embedding(conn: sqlite3.Connection, message_id: str, vector: Iterable[float]) -> None:
    """Legacy helper that stores a single embedding as chunk 0."""
    replace_message_embeddings(conn, message_id, [vector])


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
) -> List[Tuple[Message, List[float], int]]:
    if thread_id:
        cur = conn.execute(
            """
            SELECT m.id, m.thread_id, m.role, m.content, m.resource_id, m.created_at, e.vector, e.chunk_index
            FROM messages m JOIN message_embeddings e ON m.id = e.message_id
            WHERE m.thread_id=?
            ORDER BY m.created_at ASC, e.chunk_index ASC
            """,
            (thread_id,),
        )
    elif resource_id:
        cur = conn.execute(
            """
            SELECT m.id, m.thread_id, m.role, m.content, m.resource_id, m.created_at, e.vector, e.chunk_index
            FROM messages m JOIN message_embeddings e ON m.id = e.message_id
            WHERE m.resource_id=?
            ORDER BY m.created_at ASC, e.chunk_index ASC
            """,
            (resource_id,),
        )
    else:
        cur = conn.execute(
            """
            SELECT m.id, m.thread_id, m.role, m.content, m.resource_id, m.created_at, e.vector, e.chunk_index
            FROM messages m JOIN message_embeddings e ON m.id = e.message_id
            ORDER BY m.created_at ASC, e.chunk_index ASC
            """
        )

    rows = cur.fetchall()
    out: List[Tuple[Message, List[float], int]] = []
    for row in rows:
        msg = Message(*row[:6])
        vec_raw = json.loads(row[6])
        if isinstance(vec_raw, list) and vec_raw and isinstance(vec_raw[0], list):
            # Legacy multi-vector stored in legacy embeddings table.
            for idx, entry in enumerate(vec_raw):
                vec = [float(v) for v in entry]
                out.append((msg, vec, idx))
        else:
            vec = [float(v) for v in vec_raw]
            chunk_index = int(row[7]) if len(row) > 7 else 0
            out.append((msg, vec, chunk_index))
    return out


def get_messages_around(
    conn: sqlite3.Connection, thread_id: str, message_id: str, before: int, after: int
) -> List[Message]:
    if before <= 0 and after <= 0:
        return []

    cur = conn.execute(
        "SELECT rowid FROM messages WHERE id=? AND thread_id=?",
        (message_id, thread_id),
    )
    row = cur.fetchone()
    if not row:
        return []
    anchor_rowid = int(row[0])

    before_rows: List[Message] = []
    if before > 0:
        cur = conn.execute(
            "SELECT id, thread_id, role, content, resource_id, created_at FROM messages WHERE thread_id=? AND rowid < ? ORDER BY rowid DESC LIMIT ?",
            (thread_id, anchor_rowid, before),
        )
        before_rows = [Message(*r) for r in cur.fetchall()][::-1]

    after_rows: List[Message] = []
    if after > 0:
        cur = conn.execute(
            "SELECT id, thread_id, role, content, resource_id, created_at FROM messages WHERE thread_id=? AND rowid > ? ORDER BY rowid ASC LIMIT ?",
            (thread_id, anchor_rowid, after),
        )
        after_rows = [Message(*r) for r in cur.fetchall()]

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
