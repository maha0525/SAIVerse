"""Memopedia maintenance SQL access helpers."""

from __future__ import annotations

import sqlite3
from typing import List, Optional

from sai_memory.memopedia.storage import get_children, update_page


def select_target_page_ids(
    conn: sqlite3.Connection,
    since: Optional[int] = None,
    page_id: Optional[str] = None,
) -> List[str]:
    """Return target page IDs filtered by updated_at and/or page id."""
    conditions = ["is_deleted = 0", "id NOT LIKE 'root_%'"]
    params: List[object] = []

    if since is not None:
        conditions.append("updated_at >= ?")
        params.append(since)
    if page_id:
        conditions.append("id = ?")
        params.append(page_id)

    query = (
        "SELECT id FROM memopedia_pages "
        f"WHERE {' AND '.join(conditions)} "
        "ORDER BY updated_at DESC"
    )
    rows = conn.execute(query, tuple(params)).fetchall()
    return [row[0] for row in rows]


def count_target_pages(conn: sqlite3.Connection, page_ids: List[str]) -> int:
    """Count target pages for summary output."""
    return len(page_ids)


def fetch_children(conn: sqlite3.Connection, page_id: str):
    """Fetch direct child pages."""
    return get_children(conn, page_id)


def reparent_page(conn: sqlite3.Connection, page_id: str, parent_id: str) -> None:
    """Move page to another parent."""
    update_page(conn, page_id, parent_id=parent_id)
