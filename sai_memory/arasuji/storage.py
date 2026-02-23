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
    """Get an arasuji entry by ID (exact match, with prefix fallback)."""
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
    if row:
        return _row_to_entry(row)

    # Fallback: prefix match for truncated IDs (e.g. first 8 chars)
    if len(entry_id) < 36:
        cur = conn.execute(
            """
            SELECT id, level, content, source_ids_json, start_time, end_time,
                   source_count, message_count, parent_id, is_consolidated, created_at
            FROM arasuji_entries
            WHERE id LIKE ?
            LIMIT 1
            """,
            (f"{entry_id}%",),
        )
        row = cur.fetchone()
        return _row_to_entry(row) if row else None

    return None


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


def update_entry_content(
    conn: sqlite3.Connection,
    entry_id: str,
    content: str,
) -> bool:
    """Update the content of an arasuji entry.

    Args:
        conn: Database connection
        entry_id: ID of the entry to update
        content: New content text

    Returns:
        True if the entry was found and updated, False otherwise
    """
    cur = conn.execute(
        "UPDATE arasuji_entries SET content = ? WHERE id = ?",
        (content, entry_id),
    )
    conn.commit()
    return cur.rowcount > 0


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
    """Delete an arasuji entry.

    If the entry is level 2+, its child entries (referenced by source_ids)
    are automatically reset: ``is_consolidated`` → 0, ``parent_id`` → NULL.
    This ensures children become eligible for re-consolidation.
    """
    entry = get_entry(conn, entry_id)
    if not entry:
        return False

    # Reset children when deleting a consolidated parent
    if entry.level >= 2 and entry.source_ids:
        placeholders = ",".join("?" for _ in entry.source_ids)
        conn.execute(
            f"UPDATE arasuji_entries SET is_consolidated = 0, parent_id = NULL "
            f"WHERE id IN ({placeholders})",
            entry.source_ids,
        )

    conn.execute("DELETE FROM arasuji_entries WHERE id = ?", (entry_id,))
    conn.commit()
    return True


def delete_entry_and_update_parent(
    conn: sqlite3.Connection, 
    entry_id: str
) -> tuple[bool, Optional[str]]:
    """Delete entry and remove from parent's source_ids.
    
    Returns:
        (success, parent_id) - parent_id is None if no parent existed
    """
    # Get entry to find parent
    entry = get_entry(conn, entry_id)
    if not entry:
        return False, None
    
    parent_id = entry.parent_id
    
    # Update parent's source_ids if exists
    if parent_id:
        parent = get_entry(conn, parent_id)
        if parent:
            new_source_ids = [sid for sid in parent.source_ids if sid != entry_id]
            conn.execute(
                "UPDATE arasuji_entries SET source_ids_json = ? WHERE id = ?",
                (json.dumps(new_source_ids), parent_id)
            )
    
    # Delete entry
    conn.execute("DELETE FROM arasuji_entries WHERE id = ?", (entry_id,))
    conn.commit()
    
    return True, parent_id


def add_to_parent_source_ids(
    conn: sqlite3.Connection,
    entry_id: str,
    parent_id: str
) -> bool:
    """Add entry to parent's source_ids and mark as consolidated.
    
    Args:
        entry_id: ID of entry to add to parent
        parent_id: ID of parent entry
        
    Returns:
        True if successful, False if parent not found
    """
    parent = get_entry(conn, parent_id)
    if not parent:
        return False
    
    # Add to parent's source_ids
    new_source_ids = parent.source_ids + [entry_id]
    conn.execute(
        "UPDATE arasuji_entries SET source_ids_json = ? WHERE id = ?",
        (json.dumps(new_source_ids), parent_id)
    )
    
    # Mark entry as consolidated
    conn.execute(
        "UPDATE arasuji_entries SET is_consolidated = 1, parent_id = ? WHERE id = ?",
        (parent_id, entry_id)
    )
    
    conn.commit()
    return True


def dismantle_entry(
    conn: sqlite3.Connection,
    entry_id: str,
) -> Tuple[bool, List[str]]:
    """Dismantle a consolidated entry, freeing its sources for re-consolidation.

    When a large gap is detected (many unprocessed messages fall within an
    existing higher-level entry's time range), the entry's summary is no
    longer representative.  This function tears it down so that its source
    entries — together with the newly generated ones — can be re-consolidated
    from scratch via the normal ``maybe_consolidate`` path.

    Steps:
        1. Reset all source children to unconsolidated
           (``is_consolidated=0, parent_id=NULL``).
        2. Remove this entry from its parent's ``source_ids`` (if any).
           If the parent has no remaining sources, recursively dismantle it.
        3. Delete this entry.

    Args:
        conn: Database connection
        entry_id: ID of the entry to dismantle

    Returns:
        ``(success, freed_entry_ids)`` — IDs of direct source entries that
        were freed (made unconsolidated).
    """
    import logging
    _logger = logging.getLogger(__name__)

    entry = get_entry(conn, entry_id)
    if not entry:
        return False, []

    freed_ids = list(entry.source_ids)

    # 1. Reset source children to unconsolidated
    if entry.source_ids:
        placeholders = ",".join("?" for _ in entry.source_ids)
        conn.execute(
            f"UPDATE arasuji_entries SET is_consolidated = 0, parent_id = NULL "
            f"WHERE id IN ({placeholders})",
            entry.source_ids,
        )

    # 2. Remove from parent's source_ids
    if entry.parent_id:
        parent = get_entry(conn, entry.parent_id)
        if parent:
            new_source_ids = [sid for sid in parent.source_ids if sid != entry_id]
            if not new_source_ids:
                # Parent has no remaining sources — dismantle it too
                _logger.info(
                    "Parent %s has no remaining sources after removing %s, "
                    "dismantling recursively",
                    entry.parent_id[:8], entry_id[:8],
                )
                dismantle_entry(conn, entry.parent_id)
            else:
                conn.execute(
                    "UPDATE arasuji_entries SET source_ids_json = ?, source_count = ? "
                    "WHERE id = ?",
                    (json.dumps(new_source_ids), len(new_source_ids), entry.parent_id),
                )

    # 3. Delete this entry
    conn.execute("DELETE FROM arasuji_entries WHERE id = ?", (entry_id,))
    conn.commit()

    _logger.info(
        "Dismantled level-%d entry %s: freed %d source entries",
        entry.level, entry_id[:8], len(freed_ids),
    )
    return True, freed_ids


def clear_all_entries(conn: sqlite3.Connection) -> int:
    """Delete all arasuji entries. Returns count of deleted entries."""
    cur = conn.execute("DELETE FROM arasuji_entries")
    conn.execute("DELETE FROM arasuji_progress")
    conn.commit()
    return cur.rowcount


def regenerate_entry(
    conn: sqlite3.Connection,
    entry_id: str,
    model_name: Optional[str] = None,
    persona_id: Optional[str] = None,
) -> Optional[ArasujiEntry]:
    """Regenerate a Chronicle entry while preserving parent relationship.
    
    This orchestrates the full regeneration process:
    1. Get existing entry and save parent info
    2. Delete entry and update parent's source_ids
    3. Get original messages
    4. Call build_arasuji.regenerate_entry_from_messages for business logic
    5. Restore parent relationship
    
    Args:
        conn: Database connection
        entry_id: ID of entry to regenerate
        model_name: Model to use (defaults to MEMORY_WEAVE_MODEL env var)
        
    Returns:
        New ArasujiEntry or None on failure
    """
    from sai_memory.memory.storage import get_message
    
    # 1. Get existing entry
    entry = get_entry(conn, entry_id)
    if not entry:
        return None
    
    if entry.level != 1:
        raise ValueError("Only level-1 entries can be regenerated")
    
    # 2. Save parent info and source message IDs
    parent_id = entry.parent_id
    source_message_ids = entry.source_ids
    
    # 3. Delete entry and update parent
    success, _ = delete_entry_and_update_parent(conn, entry_id)
    if not success:
        return None
    
    # 4. Get original messages
    messages = []
    for msg_id in source_message_ids:
        msg = get_message(conn, msg_id)
        if msg:
            messages.append(msg)
    
    if not messages:
        return None
    
    # Sort by created_at
    messages.sort(key=lambda m: m.created_at)
    
    # 5. Call scripts layer for business logic
    from scripts.build_arasuji import regenerate_entry_from_messages
    new_entry = regenerate_entry_from_messages(conn, messages, model_name, persona_id=persona_id)
    
    if not new_entry:
        return None
    
    # 6. Restore parent relationship
    if parent_id:
        add_to_parent_source_ids(conn, new_entry.id, parent_id)
    
    return new_entry


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


def has_overlapping_entries(
    conn: sqlite3.Connection,
    start_time: int,
    end_time: int,
    level: int = 1,
) -> bool:
    """Check if there are existing entries that overlap with the given time range.
    
    An entry overlaps if:
    - entry.start_time <= end_time AND entry.end_time >= start_time
    
    Args:
        conn: Database connection
        start_time: Start of time range to check
        end_time: End of time range to check
        level: Level to check (default: 1 for level-1 Chronicle)
        
    Returns:
        True if overlapping entries exist, False otherwise
    """
    cur = conn.execute(
        """
        SELECT COUNT(*) FROM arasuji_entries
        WHERE level = ?
          AND start_time <= ?
          AND end_time >= ?
        """,
        (level, end_time, start_time),
    )
    row = cur.fetchone()
    return row[0] > 0 if row else False


def find_covering_entry(
    conn: sqlite3.Connection,
    start_time: int,
    end_time: int,
    level: int,
) -> Optional[ArasujiEntry]:
    """Find an entry at the given level whose time range covers [start_time, end_time].

    Used to detect gap-fill scenarios where a new level-1 entry falls within
    an existing higher-level entry's time range.

    Args:
        conn: Database connection
        start_time: Start of time range to check
        end_time: End of time range to check
        level: Level to search at (typically 2 for gap-fill detection)

    Returns:
        Matching ArasujiEntry or None if no covering entry exists
    """
    cur = conn.execute(
        """
        SELECT id, level, content, source_ids_json, start_time, end_time,
               source_count, message_count, parent_id, is_consolidated, created_at
        FROM arasuji_entries
        WHERE level = ? AND start_time <= ? AND end_time > ?
        ORDER BY start_time ASC
        LIMIT 1
        """,
        (level, start_time, end_time),
    )
    row = cur.fetchone()
    return _row_to_entry(row) if row else None


def search_entries(
    conn: sqlite3.Connection,
    query: Optional[str] = None,
    *,
    start_time: Optional[int] = None,
    end_time: Optional[int] = None,
    level: Optional[int] = None,
    limit: int = 10,
) -> List[ArasujiEntry]:
    """Search arasuji entries by keyword (LIKE) and/or time range and level.

    Args:
        conn: Database connection
        query: Keyword to search in content (LIKE match). None to skip.
        start_time: Filter entries overlapping with this start time.
        end_time: Filter entries overlapping with this end time.
        level: Filter by specific level. None for all levels.
        limit: Maximum results to return.

    Returns:
        List of matching ArasujiEntry, newest first.
    """
    conditions = []
    params: List[Any] = []

    if query:
        # Split by whitespace and match ANY keyword (OR)
        keywords = query.split()
        if len(keywords) > 1:
            keyword_conditions = []
            for kw in keywords:
                keyword_conditions.append("content LIKE ?")
                params.append(f"%{kw}%")
            conditions.append(f"({' OR '.join(keyword_conditions)})")
        else:
            conditions.append("content LIKE ?")
            params.append(f"%{query}%")

    if start_time is not None:
        conditions.append("end_time >= ?")
        params.append(start_time)

    if end_time is not None:
        conditions.append("start_time <= ?")
        params.append(end_time)

    if level is not None:
        conditions.append("level = ?")
        params.append(level)

    where_clause = " AND ".join(conditions) if conditions else "1=1"
    sql = f"""
        SELECT id, level, content, source_ids_json, start_time, end_time,
               source_count, message_count, parent_id, is_consolidated, created_at
        FROM arasuji_entries
        WHERE {where_clause}
        ORDER BY end_time DESC
        LIMIT ?
    """
    params.append(limit)

    cur = conn.execute(sql, params)
    return [_row_to_entry(row) for row in cur.fetchall()]
