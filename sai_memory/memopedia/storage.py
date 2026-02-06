"""Memopedia database storage layer."""

from __future__ import annotations

import difflib
import json
import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

# Category constants
CATEGORY_PEOPLE = "people"
CATEGORY_TERMS = "terms"
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
        "id": "root_terms",
        "title": "用語",
        "category": CATEGORY_TERMS,
        "summary": "対話の中で特別な意味を持つ言葉や概念",
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
    vividness: str = "rough"  # vivid, rough, faint, buried
    is_trunk: bool = False  # True if this page is a trunk (category container)
    is_important: bool = False  # True if page should not decay below "rough"
    last_referenced_at: Optional[int] = None  # Timestamp of last reference (for vividness decay)
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
            "vividness": self.vividness,
            "is_trunk": self.is_trunk,
            "is_important": self.is_important,
            "last_referenced_at": self.last_referenced_at,
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


@dataclass
class PageEditHistory:
    """Represents a single edit history entry for a page."""

    id: str
    page_id: str
    edited_at: int
    diff_text: str
    ref_start_message_id: Optional[str]
    ref_end_message_id: Optional[str]
    edit_type: str  # 'create', 'update', 'append', 'delete'
    edit_source: Optional[str]  # 'ai_conversation', 'manual', 'api', etc.


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

    # Migration: add is_deleted column for soft delete
    try:
        conn.execute("SELECT is_deleted FROM memopedia_pages LIMIT 1")
    except sqlite3.OperationalError:
        conn.execute("ALTER TABLE memopedia_pages ADD COLUMN is_deleted INTEGER DEFAULT 0")

    # Migration: add vividness column
    try:
        conn.execute("SELECT vividness FROM memopedia_pages LIMIT 1")
    except sqlite3.OperationalError:
        conn.execute("ALTER TABLE memopedia_pages ADD COLUMN vividness TEXT DEFAULT 'rough'")

    # Migration: add is_trunk column for trunk pages (category containers)
    try:
        conn.execute("SELECT is_trunk FROM memopedia_pages LIMIT 1")
    except sqlite3.OperationalError:
        conn.execute("ALTER TABLE memopedia_pages ADD COLUMN is_trunk INTEGER DEFAULT 0")

    # Migration: add is_important column for vividness floor
    try:
        conn.execute("SELECT is_important FROM memopedia_pages LIMIT 1")
    except sqlite3.OperationalError:
        conn.execute("ALTER TABLE memopedia_pages ADD COLUMN is_important INTEGER DEFAULT 0")

    # Migration: add last_referenced_at column for vividness decay
    try:
        conn.execute("SELECT last_referenced_at FROM memopedia_pages LIMIT 1")
    except sqlite3.OperationalError:
        conn.execute("ALTER TABLE memopedia_pages ADD COLUMN last_referenced_at INTEGER")

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

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS memopedia_page_edit_history (
            id TEXT PRIMARY KEY,
            page_id TEXT NOT NULL,
            edited_at INTEGER NOT NULL,
            diff_text TEXT NOT NULL,
            ref_start_message_id TEXT,
            ref_end_message_id TEXT,
            edit_type TEXT NOT NULL,
            edit_source TEXT,
            FOREIGN KEY (page_id) REFERENCES memopedia_pages(id)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_memopedia_edit_history_page ON memopedia_page_edit_history(page_id)"
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

    # Get vividness (column index 9, defaults to 'rough')
    vividness = row[9] if len(row) > 9 and row[9] else "rough"

    # Get is_trunk (column index 10, defaults to False)
    is_trunk = bool(row[10]) if len(row) > 10 and row[10] else False

    # Get is_important (column index 11, defaults to False)
    is_important = bool(row[11]) if len(row) > 11 and row[11] else False

    # Get last_referenced_at (column index 12, defaults to None)
    last_referenced_at = int(row[12]) if len(row) > 12 and row[12] else None

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
        vividness=vividness,
        is_trunk=is_trunk,
        is_important=is_important,
        last_referenced_at=last_referenced_at,
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
    vividness: str = "rough",
    is_trunk: bool = False,
    page_id: Optional[str] = None,
) -> MemopediaPage:
    """Create a new page."""
    pid = page_id or str(uuid.uuid4())
    now = int(time.time())
    kw_list = keywords or []
    conn.execute(
        """
        INSERT INTO memopedia_pages (id, parent_id, title, summary, content, category, created_at, updated_at, keywords, vividness, is_trunk, is_important, last_referenced_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (pid, parent_id, title, summary, content, category, now, now, json.dumps(kw_list), vividness, int(is_trunk), 0, now),
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
        vividness=vividness,
        is_trunk=is_trunk,
        last_referenced_at=now,
    )


def get_page(conn: sqlite3.Connection, page_id: str) -> Optional[MemopediaPage]:
    """Get a page by ID."""
    cur = conn.execute(
        "SELECT id, parent_id, title, summary, content, category, created_at, updated_at, keywords, vividness, is_trunk, is_important, last_referenced_at FROM memopedia_pages WHERE id = ?",
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
    vividness: Optional[str] = None,
    is_trunk: Optional[bool] = None,
    is_important: Optional[bool] = None,
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
    new_vividness = vividness if vividness is not None else page.vividness
    new_is_trunk = is_trunk if is_trunk is not None else page.is_trunk
    new_is_important = is_important if is_important is not None else page.is_important
    new_parent_id = parent_id if parent_id is not ... else page.parent_id
    now = int(time.time())

    conn.execute(
        """
        UPDATE memopedia_pages
        SET title = ?, summary = ?, content = ?, keywords = ?, vividness = ?, is_trunk = ?, is_important = ?, parent_id = ?, updated_at = ?
        WHERE id = ?
        """,
        (new_title, new_summary, new_content, json.dumps(new_keywords), new_vividness, int(new_is_trunk), int(new_is_important), new_parent_id, now, page_id),
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
    """Get all non-deleted direct children of a page."""
    if parent_id is None:
        cur = conn.execute(
            """SELECT id, parent_id, title, summary, content, category, created_at, updated_at,
                      keywords, vividness, is_trunk, is_important, last_referenced_at
               FROM memopedia_pages
               WHERE parent_id IS NULL AND (is_deleted = 0 OR is_deleted IS NULL)
               ORDER BY title""",
        )
    else:
        cur = conn.execute(
            """SELECT id, parent_id, title, summary, content, category, created_at, updated_at,
                      keywords, vividness, is_trunk, is_important, last_referenced_at
               FROM memopedia_pages
               WHERE parent_id = ? AND (is_deleted = 0 OR is_deleted IS NULL)
               ORDER BY title""",
            (parent_id,),
        )
    return [_row_to_page(row) for row in cur.fetchall()]


def get_all_pages(conn: sqlite3.Connection) -> List[MemopediaPage]:
    """Get all non-deleted pages."""
    cur = conn.execute(
        "SELECT id, parent_id, title, summary, content, category, created_at, updated_at, keywords, vividness, is_trunk, is_important, last_referenced_at FROM memopedia_pages WHERE is_deleted = 0 OR is_deleted IS NULL ORDER BY category, title"
    )
    return [_row_to_page(row) for row in cur.fetchall()]


def get_pages_by_category(conn: sqlite3.Connection, category: str) -> List[MemopediaPage]:
    """Get all non-deleted pages in a category."""
    cur = conn.execute(
        """SELECT id, parent_id, title, summary, content, category, created_at, updated_at,
                  keywords, vividness, is_trunk, is_important, last_referenced_at
           FROM memopedia_pages
           WHERE category = ? AND (is_deleted = 0 OR is_deleted IS NULL)
           ORDER BY title""",
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
        CATEGORY_TERMS: [],
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
        SELECT p.id, p.parent_id, p.title, p.summary, p.content, p.category, p.created_at, p.updated_at, p.keywords, p.vividness, p.is_trunk, p.last_referenced_at
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
    """Find a non-deleted page by exact title match, optionally filtered by category."""
    if category:
        cur = conn.execute(
            """SELECT id, parent_id, title, summary, content, category, created_at, updated_at,
                      keywords, vividness, is_trunk, is_important, last_referenced_at
               FROM memopedia_pages
               WHERE title = ? AND category = ? AND (is_deleted = 0 OR is_deleted IS NULL)""",
            (title, category),
        )
    else:
        cur = conn.execute(
            """SELECT id, parent_id, title, summary, content, category, created_at, updated_at,
                      keywords, vividness, is_trunk, is_important, last_referenced_at
               FROM memopedia_pages
               WHERE title = ? AND (is_deleted = 0 OR is_deleted IS NULL)""",
            (title,),
        )
    row = cur.fetchone()
    if row is None:
        return None
    return _row_to_page(row)


def search_pages(conn: sqlite3.Connection, query: str, limit: int = 10) -> List[MemopediaPage]:
    """Search non-deleted pages by title or content (simple LIKE search)."""
    pattern = f"%{query}%"
    cur = conn.execute(
        """
        SELECT id, parent_id, title, summary, content, category, created_at, updated_at,
               keywords, vividness, is_trunk, is_important, last_referenced_at
        FROM memopedia_pages
        WHERE (title LIKE ? OR summary LIKE ? OR content LIKE ?)
          AND (is_deleted = 0 OR is_deleted IS NULL)
        ORDER BY updated_at DESC
        LIMIT ?
        """,
        (pattern, pattern, pattern, limit),
    )
    return [_row_to_page(row) for row in cur.fetchall()]


# ----- Edit history operations -----


def generate_diff(old_content: str, new_content: str, context_lines: int = 3) -> str:
    """Generate a unified diff between old and new content."""
    old_lines = old_content.splitlines(keepends=True)
    new_lines = new_content.splitlines(keepends=True)
    diff = difflib.unified_diff(
        old_lines,
        new_lines,
        fromfile="before",
        tofile="after",
        lineterm="",
        n=context_lines,
    )
    return "".join(diff)


def record_page_edit(
    conn: sqlite3.Connection,
    *,
    page_id: str,
    diff_text: str,
    edit_type: str,
    ref_start_message_id: Optional[str] = None,
    ref_end_message_id: Optional[str] = None,
    edit_source: Optional[str] = None,
) -> str:
    """Record an edit history entry for a page."""
    edit_id = str(uuid.uuid4())
    now = int(time.time())
    conn.execute(
        """
        INSERT INTO memopedia_page_edit_history
        (id, page_id, edited_at, diff_text, ref_start_message_id, ref_end_message_id, edit_type, edit_source)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (edit_id, page_id, now, diff_text, ref_start_message_id, ref_end_message_id, edit_type, edit_source),
    )
    conn.commit()
    return edit_id


def get_page_edit_history(
    conn: sqlite3.Connection,
    page_id: str,
    limit: int = 50,
) -> List[PageEditHistory]:
    """Get the edit history for a page, ordered by most recent first."""
    cur = conn.execute(
        """
        SELECT id, page_id, edited_at, diff_text, ref_start_message_id, ref_end_message_id, edit_type, edit_source
        FROM memopedia_page_edit_history
        WHERE page_id = ?
        ORDER BY edited_at DESC
        LIMIT ?
        """,
        (page_id, limit),
    )
    return [
        PageEditHistory(
            id=row[0],
            page_id=row[1],
            edited_at=row[2],
            diff_text=row[3],
            ref_start_message_id=row[4],
            ref_end_message_id=row[5],
            edit_type=row[6],
            edit_source=row[7],
        )
        for row in cur.fetchall()
    ]


def get_edit_by_id(conn: sqlite3.Connection, edit_id: str) -> Optional[PageEditHistory]:
    """Get a single edit history entry by ID."""
    cur = conn.execute(
        """
        SELECT id, page_id, edited_at, diff_text, ref_start_message_id, ref_end_message_id, edit_type, edit_source
        FROM memopedia_page_edit_history
        WHERE id = ?
        """,
        (edit_id,),
    )
    row = cur.fetchone()
    if row is None:
        return None
    return PageEditHistory(
        id=row[0],
        page_id=row[1],
        edited_at=row[2],
        diff_text=row[3],
        ref_start_message_id=row[4],
        ref_end_message_id=row[5],
        edit_type=row[6],
        edit_source=row[7],
    )


# ----- Trunk operations -----


def set_trunk_flag(conn: sqlite3.Connection, page_id: str, is_trunk: bool) -> Optional[MemopediaPage]:
    """Set or unset the trunk flag for a page."""
    page = get_page(conn, page_id)
    if page is None:
        return None

    now = int(time.time())
    conn.execute(
        "UPDATE memopedia_pages SET is_trunk = ?, updated_at = ? WHERE id = ?",
        (int(is_trunk), now, page_id),
    )
    conn.commit()
    return get_page(conn, page_id)


def set_important_flag(conn: sqlite3.Connection, page_id: str, is_important: bool) -> Optional[MemopediaPage]:
    """Set or unset the important flag for a page."""
    page = get_page(conn, page_id)
    if page is None:
        return None

    now = int(time.time())
    conn.execute(
        "UPDATE memopedia_pages SET is_important = ?, updated_at = ? WHERE id = ?",
        (int(is_important), now, page_id),
    )
    conn.commit()
    return get_page(conn, page_id)


def get_trunks(conn: sqlite3.Connection, category: Optional[str] = None) -> List[MemopediaPage]:
    """Get all trunk pages, optionally filtered by category."""
    if category:
        cur = conn.execute(
            """
            SELECT id, parent_id, title, summary, content, category, created_at, updated_at, keywords, vividness, is_trunk, is_important, last_referenced_at
            FROM memopedia_pages
            WHERE is_trunk = 1 AND (is_deleted = 0 OR is_deleted IS NULL) AND category = ?
            ORDER BY title
            """,
            (category,),
        )
    else:
        cur = conn.execute(
            """
            SELECT id, parent_id, title, summary, content, category, created_at, updated_at, keywords, vividness, is_trunk, is_important, last_referenced_at
            FROM memopedia_pages
            WHERE is_trunk = 1 AND (is_deleted = 0 OR is_deleted IS NULL)
            ORDER BY category, title
            """
        )
    return [_row_to_page(row) for row in cur.fetchall()]


def move_pages_to_parent(
    conn: sqlite3.Connection,
    page_ids: List[str],
    new_parent_id: str,
) -> int:
    """
    Move multiple pages to a new parent (trunk).
    Returns the number of pages successfully moved.
    """
    # Verify the new parent exists
    parent = get_page(conn, new_parent_id)
    if parent is None:
        raise ValueError(f"Parent page not found: {new_parent_id}")

    now = int(time.time())
    moved_count = 0

    for page_id in page_ids:
        # Skip if trying to move a page to itself or to its own descendant
        if page_id == new_parent_id:
            continue

        page = get_page(conn, page_id)
        if page is None:
            continue

        # Check for circular reference (don't allow moving a page under its own descendant)
        if _is_descendant_of(conn, new_parent_id, page_id):
            continue

        # Update the parent_id
        conn.execute(
            "UPDATE memopedia_pages SET parent_id = ?, updated_at = ? WHERE id = ?",
            (new_parent_id, now, page_id),
        )
        moved_count += 1

    conn.commit()
    return moved_count


def _is_descendant_of(conn: sqlite3.Connection, potential_descendant_id: str, ancestor_id: str) -> bool:
    """Check if potential_descendant_id is a descendant of ancestor_id."""
    current_id = potential_descendant_id
    visited = set()

    while current_id:
        if current_id in visited:
            # Circular reference detected
            return False
        visited.add(current_id)

        if current_id == ancestor_id:
            return True

        page = get_page(conn, current_id)
        if page is None:
            return False
        current_id = page.parent_id

    return False


def get_unorganized_pages(conn: sqlite3.Connection, category: str) -> List[MemopediaPage]:
    """
    Get pages that are direct children of the root page (not organized into trunks).
    These are pages whose parent_id is the root page of the category.
    """
    root_id = f"root_{category}"
    cur = conn.execute(
        """
        SELECT id, parent_id, title, summary, content, category, created_at, updated_at, keywords, vividness, is_trunk, is_important, last_referenced_at
        FROM memopedia_pages
        WHERE parent_id = ? AND is_trunk = 0 AND (is_deleted = 0 OR is_deleted IS NULL)
        ORDER BY title
        """,
        (root_id,),
    )
    return [_row_to_page(row) for row in cur.fetchall()]

