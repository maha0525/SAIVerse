"""Arasuji (episode memory) database storage layer."""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple


@dataclass
class ArasujiEntry:
    """Represents a single arasuji (summary) entry."""

    id: str
    level: int  # 1=arasuji, 2=arasuji-no-arasuji, ...
    content: str
    source_ids: List[str]  # message IDs (level 1) or child arasuji IDs (level 2+)
    start_time: Optional[int]
    end_time: Optional[int]
    source_count: int  # number of sources (batch_size or consolidation_size)
    message_count: int  # total messages covered
    parent_id: Optional[str]  # parent arasuji ID if consolidated
    is_consolidated: bool
    created_at: int

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "level": self.level,
            "content": self.content,
            "source_ids": self.source_ids,
            "start_time": self.start_time,
            "end_time": self.end_time,
            "source_count": self.source_count,
            "message_count": self.message_count,
            "parent_id": self.parent_id,
            "is_consolidated": self.is_consolidated,
            "created_at": self.created_at,
        }


@dataclass
class ArasujiProgress:
    """Tracks arasuji generation progress."""

    id: str
    last_processed_message_id: Optional[str]
    last_processed_at: Optional[int]


def init_arasuji_tables(conn: sqlite3.Connection) -> None:
    """Initialize arasuji tables if they don't exist."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS arasuji_entries (
            id TEXT PRIMARY KEY,
            level INTEGER NOT NULL,
            content TEXT NOT NULL,
            source_ids_json TEXT NOT NULL,
            start_time INTEGER,
            end_time INTEGER,
            source_count INTEGER NOT NULL,
            message_count INTEGER NOT NULL,
            parent_id TEXT,
            is_consolidated INTEGER DEFAULT 0,
            created_at INTEGER NOT NULL,
            FOREIGN KEY (parent_id) REFERENCES arasuji_entries(id)
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_arasuji_level ON arasuji_entries(level)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_arasuji_end_time ON arasuji_entries(end_time DESC)")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_arasuji_consolidated ON arasuji_entries(is_consolidated)"
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_arasuji_parent ON arasuji_entries(parent_id)")

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS arasuji_progress (
            id TEXT PRIMARY KEY DEFAULT 'main',
            last_processed_message_id TEXT,
            last_processed_at INTEGER
        )
        """
    )

    conn.commit()


def _row_to_entry(row: Tuple[Any, ...]) -> ArasujiEntry:
    """Convert a database row to an ArasujiEntry object."""
    source_ids_json = row[3]
    try:
        source_ids = json.loads(source_ids_json) if source_ids_json else []
    except (json.JSONDecodeError, TypeError):
        source_ids = []

    return ArasujiEntry(
        id=row[0],
        level=int(row[1]),
        content=row[2],
        source_ids=source_ids,
        start_time=int(row[4]) if row[4] is not None else None,
        end_time=int(row[5]) if row[5] is not None else None,
        source_count=int(row[6]),
        message_count=int(row[7]),
        parent_id=row[8],
        is_consolidated=bool(row[9]),
        created_at=int(row[10]),
    )


# ----- Entry CRUD operations -----


def create_entry(
    conn: sqlite3.Connection,
    *,
    level: int,
    content: str,
    source_ids: List[str],
    start_time: Optional[int] = None,
    end_time: Optional[int] = None,
    source_count: int,
    message_count: int,
    entry_id: Optional[str] = None,
) -> ArasujiEntry:
    """Create a new arasuji entry."""
    eid = entry_id or str(uuid.uuid4())
    now = int(time.time())
    conn.execute(
        """
        INSERT INTO arasuji_entries (
            id, level, content, source_ids_json, start_time, end_time,
            source_count, message_count, parent_id, is_consolidated, created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, 0, ?)
        """,
        (
            eid,
            level,
            content,
            json.dumps(source_ids, ensure_ascii=False),
            start_time,
            end_time,
            source_count,
            message_count,
            now,
        ),
    )
    conn.commit()
    return ArasujiEntry(
        id=eid,
        level=level,
        content=content,
        source_ids=source_ids,
        start_time=start_time,
        end_time=end_time,
        source_count=source_count,
        message_count=message_count,
        parent_id=None,
        is_consolidated=False,
        created_at=now,
    )


def get_entry(conn: sqlite3.Connection, entry_id: str) -> Optional[ArasujiEntry]:
    """Get an arasuji entry by ID."""
    cur = conn.execute(
        """
        SELECT id, level, content, source_ids_json, start_time, end_time,
               source_count, message_count, parent_id, is_consolidated, created_at
        FROM arasuji_entries
        WHERE id = ?
        """,
        (entry_id,),
    )
    row = cur.fetchone()
    return _row_to_entry(row) if row else None


def get_entries_by_level(
    conn: sqlite3.Connection,
    level: int,
    *,
    only_unconsolidated: bool = False,
    order_by_time: bool = True,
) -> List[ArasujiEntry]:
    """Get all arasuji entries at a specific level."""
    query = """
        SELECT id, level, content, source_ids_json, start_time, end_time,
               source_count, message_count, parent_id, is_consolidated, created_at
        FROM arasuji_entries
        WHERE level = ?
    """
    params: List[Any] = [level]

    if only_unconsolidated:
        query += " AND is_consolidated = 0"

    if order_by_time:
        query += " ORDER BY end_time ASC"

    cur = conn.execute(query, params)
    return [_row_to_entry(row) for row in cur.fetchall()]


def get_unconsolidated_entries(
    conn: sqlite3.Connection,
    level: int,
    limit: Optional[int] = None,
) -> List[ArasujiEntry]:
    """Get unconsolidated entries at a specific level, ordered by time."""
    query = """
        SELECT id, level, content, source_ids_json, start_time, end_time,
               source_count, message_count, parent_id, is_consolidated, created_at
        FROM arasuji_entries
        WHERE level = ? AND is_consolidated = 0
        ORDER BY end_time ASC
    """
    params: List[Any] = [level]

    if limit is not None:
        query += " LIMIT ?"
        params.append(limit)

    cur = conn.execute(query, params)
    return [_row_to_entry(row) for row in cur.fetchall()]


def get_leaf_entries_by_level(conn: sqlite3.Connection, level: int) -> List[ArasujiEntry]:
    """Get 'leaf' entries at a level (not consolidated into higher level)."""
    return get_entries_by_level(conn, level, only_unconsolidated=True)


def mark_consolidated(
    conn: sqlite3.Connection,
    entry_ids: List[str],
    parent_id: str,
) -> None:
    """Mark entries as consolidated into a parent entry."""
    if not entry_ids:
        return
    placeholders = ",".join("?" for _ in entry_ids)
    conn.execute(
        f"""
        UPDATE arasuji_entries
        SET is_consolidated = 1, parent_id = ?
        WHERE id IN ({placeholders})
        """,
        [parent_id] + entry_ids,
    )
    conn.commit()


def get_all_entries_ordered(
    conn: sqlite3.Connection,
    *,
    limit: Optional[int] = None,
) -> List[ArasujiEntry]:
    """Get all entries ordered by end_time descending (newest first)."""
    query = """
        SELECT id, level, content, source_ids_json, start_time, end_time,
               source_count, message_count, parent_id, is_consolidated, created_at
        FROM arasuji_entries
        ORDER BY end_time DESC
    """
    if limit is not None:
        query += f" LIMIT {limit}"

    cur = conn.execute(query)
    return [_row_to_entry(row) for row in cur.fetchall()]


def count_entries_by_level(conn: sqlite3.Connection) -> Dict[int, int]:
    """Get count of entries per level."""
    cur = conn.execute(
        """
        SELECT level, COUNT(*) as cnt
        FROM arasuji_entries
        GROUP BY level
        ORDER BY level
        """
    )
    return {row[0]: row[1] for row in cur.fetchall()}


def count_unconsolidated_by_level(conn: sqlite3.Connection) -> Dict[int, int]:
    """Get count of unconsolidated entries per level."""
    cur = conn.execute(
        """
        SELECT level, COUNT(*) as cnt
        FROM arasuji_entries
        WHERE is_consolidated = 0
        GROUP BY level
        ORDER BY level
        """
    )
    return {row[0]: row[1] for row in cur.fetchall()}


def get_max_level(conn: sqlite3.Connection) -> int:
    """Get the maximum level of arasuji entries."""
    cur = conn.execute("SELECT MAX(level) FROM arasuji_entries")
    row = cur.fetchone()
    return row[0] if row and row[0] is not None else 0


def delete_entry(conn: sqlite3.Connection, entry_id: str) -> bool:
    """Delete an arasuji entry."""
    cur = conn.execute("DELETE FROM arasuji_entries WHERE id = ?", (entry_id,))
    conn.commit()
    return cur.rowcount > 0


def clear_all_entries(conn: sqlite3.Connection) -> int:
    """Delete all arasuji entries. Returns count of deleted entries."""
    cur = conn.execute("DELETE FROM arasuji_entries")
    conn.execute("DELETE FROM arasuji_progress")
    conn.commit()
    return cur.rowcount


# ----- Progress tracking -----


def get_progress(conn: sqlite3.Connection, progress_id: str = "main") -> Optional[ArasujiProgress]:
    """Get arasuji generation progress."""
    cur = conn.execute(
        "SELECT id, last_processed_message_id, last_processed_at FROM arasuji_progress WHERE id = ?",
        (progress_id,),
    )
    row = cur.fetchone()
    if row:
        return ArasujiProgress(
            id=row[0],
            last_processed_message_id=row[1],
            last_processed_at=int(row[2]) if row[2] is not None else None,
        )
    return None


def update_progress(
    conn: sqlite3.Connection,
    last_processed_message_id: str,
    progress_id: str = "main",
) -> None:
    """Update arasuji generation progress."""
    now = int(time.time())
    conn.execute(
        """
        INSERT INTO arasuji_progress (id, last_processed_message_id, last_processed_at)
        VALUES (?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            last_processed_message_id = excluded.last_processed_message_id,
            last_processed_at = excluded.last_processed_at
        """,
        (progress_id, last_processed_message_id, now),
    )
    conn.commit()


# ----- Utility functions for context retrieval -----


def get_entries_ending_before(
    conn: sqlite3.Connection,
    end_time: int,
    level: int,
    *,
    limit: int = 10,
) -> List[ArasujiEntry]:
    """Get entries at a level ending before a specific time."""
    cur = conn.execute(
        """
        SELECT id, level, content, source_ids_json, start_time, end_time,
               source_count, message_count, parent_id, is_consolidated, created_at
        FROM arasuji_entries
        WHERE level = ? AND end_time < ?
        ORDER BY end_time DESC
        LIMIT ?
        """,
        (level, end_time, limit),
    )
    return [_row_to_entry(row) for row in cur.fetchall()]


def get_latest_entry_at_level(
    conn: sqlite3.Connection,
    level: int,
    *,
    only_unconsolidated: bool = False,
) -> Optional[ArasujiEntry]:
    """Get the latest (most recent) entry at a specific level."""
    query = """
        SELECT id, level, content, source_ids_json, start_time, end_time,
               source_count, message_count, parent_id, is_consolidated, created_at
        FROM arasuji_entries
        WHERE level = ?
    """
    if only_unconsolidated:
        query += " AND is_consolidated = 0"
    query += " ORDER BY end_time DESC LIMIT 1"

    cur = conn.execute(query, (level,))
    row = cur.fetchone()
    return _row_to_entry(row) if row else None


def get_children(conn: sqlite3.Connection, parent_id: str) -> List[ArasujiEntry]:
    """Get child entries of a parent arasuji."""
    cur = conn.execute(
        """
        SELECT id, level, content, source_ids_json, start_time, end_time,
               source_count, message_count, parent_id, is_consolidated, created_at
        FROM arasuji_entries
        WHERE parent_id = ?
        ORDER BY end_time ASC
        """,
        (parent_id,),
    )
    return [_row_to_entry(row) for row in cur.fetchall()]


def get_total_message_count(conn: sqlite3.Connection) -> int:
    """Get total messages covered by all level-1 entries."""
    cur = conn.execute(
        "SELECT SUM(message_count) FROM arasuji_entries WHERE level = 1"
    )
    row = cur.fetchone()
    return row[0] if row and row[0] is not None else 0
