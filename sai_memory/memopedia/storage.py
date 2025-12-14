"""Memopedia database storage layer."""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

# Category constants
CATEGORY_PEOPLE = "people"
CATEGORY_EVENTS = "events"
CATEGORY_PLANS = "plans"

INITIAL_ROOTS = [
    {
        "id": "root_people",
        "title": "人物",
        "category": CATEGORY_PEOPLE,
        "summary": "関わりのある人物についての記録",
        "content": "",
    },
    {
        "id": "root_events",
        "title": "出来事",
        "category": CATEGORY_EVENTS,
        "summary": "過去に起きた出来事の記録",
        "content": "",
    },
    {
        "id": "root_plans",
        "title": "予定",
        "category": CATEGORY_PLANS,
        "summary": "進行中や計画中のプロジェクト・予定",
        "content": "",
    },
]


@dataclass
class MemopediaPage:
    """Represents a single Memopedia page."""

    id: str
    parent_id: Optional[str]
    title: str
    summary: str
    content: str
    category: str
    created_at: int
    updated_at: int
    keywords: List[str] = field(default_factory=list)
    children: List["MemopediaPage"] = field(default_factory=list)

    def to_dict(self, include_children: bool = True) -> Dict[str, Any]:
        result = {
            "id": self.id,
            "parent_id": self.parent_id,
            "title": self.title,
            "summary": self.summary,
            "content": self.content,
            "category": self.category,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "keywords": self.keywords,
        }
        if include_children:
            result["children"] = [c.to_dict(include_children=True) for c in self.children]
        return result


@dataclass
class PageState:
    """Represents the open/close state of a page for a thread."""

    thread_id: str
    page_id: str
    is_open: bool
    opened_at: Optional[int]


def init_memopedia_tables(conn: sqlite3.Connection) -> None:
    """Initialize Memopedia tables and seed root pages if needed."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS memopedia_pages (
            id TEXT PRIMARY KEY,
            parent_id TEXT,
            title TEXT NOT NULL,
            summary TEXT DEFAULT '',
            content TEXT DEFAULT '',
            category TEXT NOT NULL,
            created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL,
            keywords TEXT DEFAULT '[]',
            FOREIGN KEY (parent_id) REFERENCES memopedia_pages(id)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_memopedia_pages_parent ON memopedia_pages(parent_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_memopedia_pages_category ON memopedia_pages(category)"
    )

    # Migration: add keywords column if it doesn't exist
    try:
        conn.execute("SELECT keywords FROM memopedia_pages LIMIT 1")
    except sqlite3.OperationalError:
        conn.execute("ALTER TABLE memopedia_pages ADD COLUMN keywords TEXT DEFAULT '[]'")

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS memopedia_page_states (
            thread_id TEXT NOT NULL,
            page_id TEXT NOT NULL,
            is_open INTEGER DEFAULT 0,
            opened_at INTEGER,
            PRIMARY KEY (thread_id, page_id),
            FOREIGN KEY (page_id) REFERENCES memopedia_pages(id)
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS memopedia_update_log (
            id TEXT PRIMARY KEY,
            last_message_id TEXT,
            last_message_created_at INTEGER,
            processed_at INTEGER NOT NULL
        )
        """
    )

    conn.commit()

    # Seed root pages if they don't exist
    _seed_root_pages(conn)


def _seed_root_pages(conn: sqlite3.Connection) -> None:
    """Create initial root pages if they don't exist."""
    now = int(time.time())
    for root in INITIAL_ROOTS:
        cur = conn.execute("SELECT id FROM memopedia_pages WHERE id = ?", (root["id"],))
        if cur.fetchone() is None:
            conn.execute(
                """
                INSERT INTO memopedia_pages (id, parent_id, title, summary, content, category, created_at, updated_at)
                VALUES (?, NULL, ?, ?, ?, ?, ?, ?)
                """,
                (root["id"], root["title"], root["summary"], root["content"], root["category"], now, now),
            )
    conn.commit()


def _row_to_page(row: tuple) -> MemopediaPage:
    """Convert a database row to a MemopediaPage object."""
    # Parse keywords JSON (column index 8)
    keywords_json = row[8] if len(row) > 8 else "[]"
    try:
        keywords = json.loads(keywords_json) if keywords_json else []
    except (json.JSONDecodeError, TypeError):
        keywords = []

    return MemopediaPage(
        id=row[0],
        parent_id=row[1],
        title=row[2],
        summary=row[3] or "",
        content=row[4] or "",
        category=row[5],
        created_at=int(row[6]),
        updated_at=int(row[7]),
        keywords=keywords,
    )


# ----- Page CRUD operations -----


def create_page(
    conn: sqlite3.Connection,
    *,
    parent_id: Optional[str],
    title: str,
    summary: str = "",
    content: str = "",
    category: str,
    keywords: Optional[List[str]] = None,
    page_id: Optional[str] = None,
) -> MemopediaPage:
    """Create a new page."""
    pid = page_id or str(uuid.uuid4())
    now = int(time.time())
    kw_list = keywords or []
    conn.execute(
        """
        INSERT INTO memopedia_pages (id, parent_id, title, summary, content, category, created_at, updated_at, keywords)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (pid, parent_id, title, summary, content, category, now, now, json.dumps(kw_list)),
    )
    conn.commit()
    return MemopediaPage(
        id=pid,
        parent_id=parent_id,
        title=title,
        summary=summary,
        content=content,
        category=category,
        created_at=now,
        updated_at=now,
        keywords=kw_list,
    )


def get_page(conn: sqlite3.Connection, page_id: str) -> Optional[MemopediaPage]:
    """Get a page by ID."""
    cur = conn.execute(
        "SELECT id, parent_id, title, summary, content, category, created_at, updated_at, keywords FROM memopedia_pages WHERE id = ?",
        (page_id,),
    )
    row = cur.fetchone()
    if row is None:
        return None
    return _row_to_page(row)


def update_page(
    conn: sqlite3.Connection,
    page_id: str,
    *,
    title: Optional[str] = None,
    summary: Optional[str] = None,
    content: Optional[str] = None,
    keywords: Optional[List[str]] = None,
    parent_id: Optional[str] = ...,  # Use ... as sentinel for "not provided"
) -> Optional[MemopediaPage]:
    """Update a page's fields. Only provided fields are updated."""
    page = get_page(conn, page_id)
    if page is None:
        return None

    new_title = title if title is not None else page.title
    new_summary = summary if summary is not None else page.summary
    new_content = content if content is not None else page.content
    new_keywords = keywords if keywords is not None else page.keywords
    new_parent_id = parent_id if parent_id is not ... else page.parent_id
    now = int(time.time())

    conn.execute(
        """
        UPDATE memopedia_pages
        SET title = ?, summary = ?, content = ?, keywords = ?, parent_id = ?, updated_at = ?
        WHERE id = ?
        """,
        (new_title, new_summary, new_content, json.dumps(new_keywords), new_parent_id, now, page_id),
    )
    conn.commit()
    return get_page(conn, page_id)


def delete_page(conn: sqlite3.Connection, page_id: str) -> bool:
    """Delete a page and all its descendants."""
    # First, recursively delete children
    children = get_children(conn, page_id)
    for child in children:
        delete_page(conn, child.id)

    # Delete page states
    conn.execute("DELETE FROM memopedia_page_states WHERE page_id = ?", (page_id,))
    # Delete the page itself
    conn.execute("DELETE FROM memopedia_pages WHERE id = ?", (page_id,))
    conn.commit()
    return True


def get_children(conn: sqlite3.Connection, parent_id: Optional[str]) -> List[MemopediaPage]:
    """Get all direct children of a page."""
    if parent_id is None:
        cur = conn.execute(
            "SELECT id, parent_id, title, summary, content, category, created_at, updated_at, keywords FROM memopedia_pages WHERE parent_id IS NULL ORDER BY title",
        )
    else:
        cur = conn.execute(
            "SELECT id, parent_id, title, summary, content, category, created_at, updated_at, keywords FROM memopedia_pages WHERE parent_id = ? ORDER BY title",
            (parent_id,),
        )
    return [_row_to_page(row) for row in cur.fetchall()]


def get_all_pages(conn: sqlite3.Connection) -> List[MemopediaPage]:
    """Get all pages."""
    cur = conn.execute(
        "SELECT id, parent_id, title, summary, content, category, created_at, updated_at, keywords FROM memopedia_pages ORDER BY category, title"
    )
    return [_row_to_page(row) for row in cur.fetchall()]


def get_pages_by_category(conn: sqlite3.Connection, category: str) -> List[MemopediaPage]:
    """Get all pages in a category."""
    cur = conn.execute(
        "SELECT id, parent_id, title, summary, content, category, created_at, updated_at, keywords FROM memopedia_pages WHERE category = ? ORDER BY title",
        (category,),
    )
    return [_row_to_page(row) for row in cur.fetchall()]


def build_tree(conn: sqlite3.Connection) -> Dict[str, List[MemopediaPage]]:
    """Build the full tree structure organized by category."""
    all_pages = get_all_pages(conn)

    # Build a lookup for children
    children_map: Dict[Optional[str], List[MemopediaPage]] = {}
    for page in all_pages:
        parent = page.parent_id
        if parent not in children_map:
            children_map[parent] = []
        children_map[parent].append(page)

    def _attach_children(page: MemopediaPage) -> MemopediaPage:
        page.children = children_map.get(page.id, [])
        for child in page.children:
            _attach_children(child)
        return page

    # Get root pages and attach children recursively
    roots = children_map.get(None, [])
    for root in roots:
        _attach_children(root)

    # Organize by category
    result: Dict[str, List[MemopediaPage]] = {
        CATEGORY_PEOPLE: [],
        CATEGORY_EVENTS: [],
        CATEGORY_PLANS: [],
    }
    for root in roots:
        if root.category in result:
            result[root.category].append(root)

    return result


# ----- Page state operations -----


def get_page_state(conn: sqlite3.Connection, thread_id: str, page_id: str) -> PageState:
    """Get the open/close state of a page for a thread."""
    cur = conn.execute(
        "SELECT thread_id, page_id, is_open, opened_at FROM memopedia_page_states WHERE thread_id = ? AND page_id = ?",
        (thread_id, page_id),
    )
    row = cur.fetchone()
    if row is None:
        return PageState(thread_id=thread_id, page_id=page_id, is_open=False, opened_at=None)
    return PageState(
        thread_id=row[0],
        page_id=row[1],
        is_open=bool(row[2]),
        opened_at=row[3],
    )


def set_page_open(conn: sqlite3.Connection, thread_id: str, page_id: str, is_open: bool) -> PageState:
    """Set the open/close state of a page for a thread."""
    now = int(time.time()) if is_open else None
    conn.execute(
        """
        INSERT INTO memopedia_page_states (thread_id, page_id, is_open, opened_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(thread_id, page_id) DO UPDATE SET is_open = ?, opened_at = ?
        """,
        (thread_id, page_id, int(is_open), now, int(is_open), now),
    )
    conn.commit()
    return PageState(thread_id=thread_id, page_id=page_id, is_open=is_open, opened_at=now)


def get_open_pages(conn: sqlite3.Connection, thread_id: str) -> List[MemopediaPage]:
    """Get all pages that are currently open for a thread."""
    cur = conn.execute(
        """
        SELECT p.id, p.parent_id, p.title, p.summary, p.content, p.category, p.created_at, p.updated_at
        FROM memopedia_pages p
        JOIN memopedia_page_states s ON p.id = s.page_id
        WHERE s.thread_id = ? AND s.is_open = 1
        ORDER BY s.opened_at ASC
        """,
        (thread_id,),
    )
    return [_row_to_page(row) for row in cur.fetchall()]


def get_all_states_for_thread(conn: sqlite3.Connection, thread_id: str) -> Dict[str, bool]:
    """Get all page states for a thread as a dict of page_id -> is_open."""
    cur = conn.execute(
        "SELECT page_id, is_open FROM memopedia_page_states WHERE thread_id = ?",
        (thread_id,),
    )
    return {row[0]: bool(row[1]) for row in cur.fetchall()}


# ----- Update log operations -----


def get_last_update_log(conn: sqlite3.Connection) -> Optional[Dict[str, Any]]:
    """Get the most recent update log entry."""
    cur = conn.execute(
        "SELECT id, last_message_id, last_message_created_at, processed_at FROM memopedia_update_log ORDER BY processed_at DESC LIMIT 1"
    )
    row = cur.fetchone()
    if row is None:
        return None
    return {
        "id": row[0],
        "last_message_id": row[1],
        "last_message_created_at": row[2],
        "processed_at": row[3],
    }


def record_update_log(
    conn: sqlite3.Connection,
    *,
    last_message_id: Optional[str],
    last_message_created_at: Optional[int],
) -> str:
    """Record a new update log entry."""
    log_id = str(uuid.uuid4())
    now = int(time.time())
    conn.execute(
        """
        INSERT INTO memopedia_update_log (id, last_message_id, last_message_created_at, processed_at)
        VALUES (?, ?, ?, ?)
        """,
        (log_id, last_message_id, last_message_created_at, now),
    )
    conn.commit()
    return log_id


def find_page_by_title(conn: sqlite3.Connection, title: str, category: Optional[str] = None) -> Optional[MemopediaPage]:
    """Find a page by exact title match, optionally filtered by category."""
    if category:
        cur = conn.execute(
            "SELECT id, parent_id, title, summary, content, category, created_at, updated_at, keywords FROM memopedia_pages WHERE title = ? AND category = ?",
            (title, category),
        )
    else:
        cur = conn.execute(
            "SELECT id, parent_id, title, summary, content, category, created_at, updated_at, keywords FROM memopedia_pages WHERE title = ?",
            (title,),
        )
    row = cur.fetchone()
    if row is None:
        return None
    return _row_to_page(row)


def search_pages(conn: sqlite3.Connection, query: str, limit: int = 10) -> List[MemopediaPage]:
    """Search pages by title or content (simple LIKE search)."""
    pattern = f"%{query}%"
    cur = conn.execute(
        """
        SELECT id, parent_id, title, summary, content, category, created_at, updated_at
        FROM memopedia_pages
        WHERE title LIKE ? OR summary LIKE ? OR content LIKE ?
        ORDER BY updated_at DESC
        LIMIT ?
        """,
        (pattern, pattern, pattern, limit),
    )
    return [_row_to_page(row) for row in cur.fetchall()]
