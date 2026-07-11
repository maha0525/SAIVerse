"""Memopedia database storage layer."""

from __future__ import annotations

import difflib
import json
import logging
import re
import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

LOGGER = logging.getLogger(__name__)

# Category constants (後方互換のため残す — 新規コードは CATEGORY_DEFS を使うこと)
CATEGORY_PEOPLE = "people"
CATEGORY_TERMS = "terms"
CATEGORY_PLANS = "plans"
CATEGORY_EVENTS = "events"
# テーマ (旧 Note の後継、P3c①)。entity_extractor の抽出対象4カテゴリには
# 含めない — テーマは本人が立てるもので、抽出で自動生成しない
# (新規テーマの命名は P4 代謝の領分。sai_memory/theme_pages.py 参照)。
CATEGORY_THEME = "theme"


@dataclass(frozen=True)
class CategoryDef:
    """カテゴリ定義。全フラグはここが唯一の真実の源。"""

    key: str
    label: str
    label_en: str
    order: int
    in_tree: bool = True
    hide_when_empty: bool = False
    extractable: bool = False
    writable: bool = False
    metabolizable: bool = False


CATEGORY_DEFS: Dict[str, CategoryDef] = {
    "people": CategoryDef("people", "人物", "People", order=1, in_tree=True, extractable=True, writable=True, metabolizable=True),
    "terms": CategoryDef("terms", "用語", "Terms", order=2, in_tree=True, extractable=True, writable=True, metabolizable=True),
    "plans": CategoryDef("plans", "計画", "Plans", order=3, in_tree=True, extractable=True, writable=True, metabolizable=True),
    "events": CategoryDef("events", "出来事", "Events", order=4, in_tree=True, extractable=True, writable=True, metabolizable=True),
    "theme": CategoryDef("theme", "テーマ", "Themes", order=5, in_tree=True, hide_when_empty=True),
    "core": CategoryDef("core", "コア記憶", "Core Memory", order=6, in_tree=False),
    "chronicle": CategoryDef("chronicle", "クロニクル", "Chronicle", order=7, in_tree=False),
}


def category_keys(role: str) -> List[str]:
    """role フラグが True のカテゴリキーを order 順で返す。

    role: "in_tree" | "extractable" | "writable" | "metabolizable" | "hide_when_empty"
    """
    return [
        d.key
        for d in sorted(CATEGORY_DEFS.values(), key=lambda d: d.order)
        if getattr(d, role, False)
    ]


def category_label(key: str) -> str:
    """カテゴリキーに対応する日本語ラベルを返す。未知キーはキーそのものを返す。"""
    return CATEGORY_DEFS[key].label if key in CATEGORY_DEFS else key


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
        "title": "計画",
        "category": CATEGORY_PLANS,
        "summary": "進行中や計画中のプロジェクト・予定",
        "content": "",
    },
    {
        "id": "root_events",
        "title": "出来事",
        "category": CATEGORY_EVENTS,
        "summary": "出来事、体験、時事的な話題",
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
    metadata: Optional[Dict[str, Any]] = None  # Additional metadata (e.g., persona_id)
    short_id: Optional[int] = None  # Per-DB sequential ID (m:1, m:2, ...)
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
            "metadata": self.metadata,
            "short_id": self.short_id,
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
    # Snapshot of the page state BEFORE this edit was applied. Required for
    # reliable rollback. NULL for legacy entries recorded before v0.3.0 — those
    # cannot be rolled back (rollback_page returns an error for such entries).
    before_title: Optional[str] = None
    before_summary: Optional[str] = None
    before_content: Optional[str] = None


@dataclass
class MemopediaFragment:
    """A single fragment of knowledge linked to an entity and optionally to a Chronicle entry."""

    id: str
    content: str
    entity_id: str
    chronicle_entry_id: Optional[str] = None
    vividness: str = "vivid"
    source_date: Optional[str] = None
    created_at: int = 0


def _backfill_short_ids(conn: sqlite3.Connection) -> None:
    """Assign short_id to existing pages that lack one (migration helper)."""
    cur = conn.execute(
        "SELECT id FROM memopedia_pages WHERE short_id IS NULL ORDER BY created_at"
    )
    rows = cur.fetchall()
    max_cur = conn.execute(
        "SELECT COALESCE(MAX(short_id), 0) FROM memopedia_pages"
    )
    next_id = max_cur.fetchone()[0] + 1
    for (page_id,) in rows:
        conn.execute(
            "UPDATE memopedia_pages SET short_id = ? WHERE id = ?",
            (next_id, page_id),
        )
        next_id += 1
    if rows:
        conn.commit()


def _next_short_id(conn: sqlite3.Connection) -> int:
    """Return the next short_id for a new page (MAX + 1, first is 1)."""
    cur = conn.execute("SELECT COALESCE(MAX(short_id), 0) FROM memopedia_pages")
    return cur.fetchone()[0] + 1


_SHORT_ID_RE = re.compile(r"^memopedia:(\d+)$", re.IGNORECASE)
# saiverse://self/memopedia/{key}  /  saiverse://{city}/{persona}/memopedia/{key}
# 参照アドレッシング統一で page/ 階層は平坦化済み。key は short_id が主・UUID も可。
_MEMOPEDIA_URI_KEY_RE = re.compile(
    r"^saiverse://[^/]+(?:/[^/]+)?/memopedia/(?P<key>[^?/]+)"
)


def resolve_page_ref(conn: sqlite3.Connection, ref: str) -> Optional[str]:
    """ページ参照を実 page_id (UUID) に解決する単一の入口。

    参照アドレッシング統一の AI 可視キーは short_id (``memopedia:N``)。この関数が
    全経路 (memopedia_note / list_fragments / get_page / open_page / uri_resolver 等)
    の入力正規化を担う。受理する形:

    - ``memopedia:N`` / 素の数字 ``N`` → short_id で page を引く (AI 可視の主形式)
    - ``saiverse://.../memopedia/{key}`` URI → key を取り出して再解決
    - UUID / prefix → そのまま (get_page 側の prefix フォールバックに委ねる)

    見つからなければ None。
    """
    ref = ref.strip()
    # URI ならキーを取り出して再帰的に解く。
    m = _MEMOPEDIA_URI_KEY_RE.match(ref)
    if m:
        ref = m.group("key")
    # memopedia:N か素の数字は short_id。
    sm = _SHORT_ID_RE.match(ref)
    if sm:
        sid = int(sm.group(1))
    elif ref.isdigit():
        sid = int(ref)
    else:
        sid = None
    if sid is not None:
        cur = conn.execute(
            "SELECT id FROM memopedia_pages WHERE short_id = ?", (sid,)
        )
        row = cur.fetchone()
        return row[0] if row else None
    return ref


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

    # Migration: add metadata column if it doesn't exist
    try:
        conn.execute("SELECT metadata FROM memopedia_pages LIMIT 1")
    except sqlite3.OperationalError:
        conn.execute("ALTER TABLE memopedia_pages ADD COLUMN metadata TEXT")

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

    # Migration: add short_id column (per-DB sequential ID for m:N references)
    try:
        conn.execute("SELECT short_id FROM memopedia_pages LIMIT 1")
    except sqlite3.OperationalError:
        conn.execute("ALTER TABLE memopedia_pages ADD COLUMN short_id INTEGER")
        _backfill_short_ids(conn)

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

    # Migration: add before-snapshot columns for reliable rollback (v0.3.0+).
    # Pre-existing rows have NULL in these columns and are not rollback-capable.
    try:
        conn.execute("SELECT before_title FROM memopedia_page_edit_history LIMIT 1")
    except sqlite3.OperationalError:
        conn.execute("ALTER TABLE memopedia_page_edit_history ADD COLUMN before_title TEXT")
        conn.execute("ALTER TABLE memopedia_page_edit_history ADD COLUMN before_summary TEXT")
        conn.execute("ALTER TABLE memopedia_page_edit_history ADD COLUMN before_content TEXT")

    # Embeddings for Memopedia pages (used by unified recall)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS memopedia_embeddings (
            page_id TEXT PRIMARY KEY,
            vector TEXT NOT NULL,
            FOREIGN KEY (page_id) REFERENCES memopedia_pages(id)
        )
        """
    )

    # Fragment tables for fine-grained knowledge linked to entity pages
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS memopedia_fragments (
            id TEXT PRIMARY KEY,
            content TEXT NOT NULL,
            entity_id TEXT NOT NULL,
            chronicle_entry_id TEXT,
            vividness TEXT DEFAULT 'vivid',
            source_date TEXT,
            created_at INTEGER NOT NULL,
            FOREIGN KEY (entity_id) REFERENCES memopedia_pages(id)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_fragments_entity ON memopedia_fragments(entity_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_fragments_chronicle ON memopedia_fragments(chronicle_entry_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_fragments_vividness ON memopedia_fragments(vividness)"
    )

    # Embeddings for Memopedia fragments (used by unified recall)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS memopedia_fragment_embeddings (
            fragment_id TEXT PRIMARY KEY,
            vector TEXT NOT NULL,
            FOREIGN KEY (fragment_id) REFERENCES memopedia_fragments(id)
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


_PAGE_SELECT_COLS = (
    "id, parent_id, title, summary, content, category, "
    "created_at, updated_at, keywords, vividness, is_trunk, "
    "is_important, last_referenced_at, metadata, short_id"
)


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

    # Parse metadata JSON (column index 13, defaults to None)
    metadata_json = row[13] if len(row) > 13 else None
    metadata = None
    if metadata_json:
        try:
            metadata = json.loads(metadata_json)
        except (json.JSONDecodeError, TypeError):
            metadata = None

    short_id = int(row[14]) if len(row) > 14 and row[14] is not None else None

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
        metadata=metadata,
        short_id=short_id,
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
    metadata: Optional[Dict[str, Any]] = None,
    page_id: Optional[str] = None,
) -> MemopediaPage:
    """Create a new page."""
    pid = page_id or str(uuid.uuid4())
    now = int(time.time())
    kw_list = keywords or []
    metadata_json = json.dumps(metadata) if metadata else None
    sid = _next_short_id(conn)
    conn.execute(
        """
        INSERT INTO memopedia_pages (id, parent_id, title, summary, content, category, created_at, updated_at, keywords, vividness, is_trunk, is_important, last_referenced_at, metadata, short_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (pid, parent_id, title, summary, content, category, now, now, json.dumps(kw_list), vividness, int(is_trunk), 0, now, metadata_json, sid),
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
        metadata=metadata,
        short_id=sid,
    )


def get_page(conn: sqlite3.Connection, page_id: str) -> Optional[MemopediaPage]:
    """Get a page by ID, short ref (m:N), or prefix fallback."""
    resolved = resolve_page_ref(conn, page_id)
    if resolved is None:
        return None
    page_id = resolved
    cur = conn.execute(
        f"SELECT {_PAGE_SELECT_COLS} FROM memopedia_pages WHERE id = ?",
        (page_id,),
    )
    row = cur.fetchone()
    if row is not None:
        return _row_to_page(row)

    # Fallback: prefix match for truncated IDs (e.g. first 8 chars)
    if len(page_id) < 36:
        cur = conn.execute(
            f"SELECT {_PAGE_SELECT_COLS} FROM memopedia_pages WHERE id LIKE ? LIMIT 1",
            (f"{page_id}%",),
        )
        row = cur.fetchone()
        return _row_to_page(row) if row else None

    return None


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
    metadata: Optional[Dict[str, Any]] = None,
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
    new_metadata = metadata if metadata is not None else page.metadata
    new_parent_id = parent_id if parent_id is not ... else page.parent_id
    now = int(time.time())

    conn.execute(
        """
        UPDATE memopedia_pages
        SET title = ?, summary = ?, content = ?, keywords = ?, vividness = ?, is_trunk = ?, is_important = ?, parent_id = ?, updated_at = ?, metadata = ?
        WHERE id = ?
        """,
        (new_title, new_summary, new_content, json.dumps(new_keywords), new_vividness, int(new_is_trunk), int(new_is_important), new_parent_id, now, json.dumps(new_metadata), page_id),
    )
    conn.commit()
    return get_page(conn, page_id)


def delete_page(conn: sqlite3.Connection, page_id: str) -> bool:
    """Delete a page and all its descendants, including fragments."""
    # First, recursively delete children
    children = get_children(conn, page_id)
    for child in children:
        delete_page(conn, child.id)

    # Delete fragment embeddings, then fragments
    conn.execute(
        "DELETE FROM memopedia_fragment_embeddings WHERE fragment_id IN "
        "(SELECT id FROM memopedia_fragments WHERE entity_id = ?)",
        (page_id,),
    )
    conn.execute("DELETE FROM memopedia_fragments WHERE entity_id = ?", (page_id,))
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
        f"SELECT {_PAGE_SELECT_COLS} FROM memopedia_pages WHERE is_deleted = 0 OR is_deleted IS NULL ORDER BY category, title"
    )
    return [_row_to_page(row) for row in cur.fetchall()]


def get_pages_by_category(conn: sqlite3.Connection, category: str) -> List[MemopediaPage]:
    """Get all non-deleted pages in a category."""
    cur = conn.execute(
        """SELECT id, parent_id, title, summary, content, category, created_at, updated_at,
                  keywords, vividness, is_trunk, is_important, last_referenced_at, metadata
           FROM memopedia_pages
           WHERE category = ? AND (is_deleted = 0 OR is_deleted IS NULL)
           ORDER BY title""",
        (category,),
    )
    return [_row_to_page(row) for row in cur.fetchall()]


def get_page_by_title(
    conn: sqlite3.Connection,
    title: str,
    *,
    category: Optional[str] = None,
) -> Optional[MemopediaPage]:
    """Get a page by exact title match.

    Args:
        conn: Database connection
        title: Page title to search for
        category: Optional category filter

    Returns:
        MemopediaPage if found, None otherwise
    """
    if category:
        cur = conn.execute(
            """SELECT id, parent_id, title, summary, content, category, created_at, updated_at,
                      keywords, vividness, is_trunk, is_important, last_referenced_at, metadata
               FROM memopedia_pages
               WHERE title = ? AND category = ? AND (is_deleted = 0 OR is_deleted IS NULL)
               LIMIT 1""",
            (title, category),
        )
    else:
        cur = conn.execute(
            """SELECT id, parent_id, title, summary, content, category, created_at, updated_at,
                      keywords, vividness, is_trunk, is_important, last_referenced_at, metadata
               FROM memopedia_pages
               WHERE title = ? AND (is_deleted = 0 OR is_deleted IS NULL)
               LIMIT 1""",
            (title,),
        )
    row = cur.fetchone()
    if row:
        return _row_to_page(row)
    return None


def get_page_by_persona_id(
    conn: sqlite3.Connection,
    persona_id: str,
) -> Optional[MemopediaPage]:
    """Get a page by persona_id in metadata.

    Args:
        conn: Database connection
        persona_id: Persona ID to search for in metadata

    Returns:
        MemopediaPage if found, None otherwise
    """
    cur = conn.execute(
        """SELECT id, parent_id, title, summary, content, category, created_at, updated_at,
                  keywords, vividness, is_trunk, is_important, last_referenced_at, metadata
           FROM memopedia_pages
           WHERE metadata IS NOT NULL
           AND json_extract(metadata, '$.persona_id') = ?
           AND (is_deleted = 0 OR is_deleted IS NULL)
           LIMIT 1""",
        (persona_id,),
    )
    row = cur.fetchone()
    if row:
        return _row_to_page(row)
    return None


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
    # (core / chronicle カテゴリの trunk は意図的に含めない — コア記憶は
    #  常時開の head 常設、Chronicle は時間の地図として別導線を持つ)
    result: Dict[str, List[MemopediaPage]] = {k: [] for k in category_keys("in_tree")}
    unknown_cats: set = set()
    for root in roots:
        if root.category in result:
            result[root.category].append(root)
        elif root.category not in CATEGORY_DEFS:
            unknown_cats.add(root.category)
    if unknown_cats:
        LOGGER.warning(
            "build_tree: unknown categories skipped (not in CATEGORY_DEFS): %s",
            sorted(unknown_cats),
        )

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
    # _PAGE_SELECT_COLS を使う (旧・手書き列リストは short_id / metadata を
    # 落としていた — search_pages と同族の欠落。P2c-2 でついで修正)
    if category:
        cur = conn.execute(
            f"""SELECT {_PAGE_SELECT_COLS}
               FROM memopedia_pages
               WHERE title = ? AND category = ? AND (is_deleted = 0 OR is_deleted IS NULL)""",
            (title, category),
        )
    else:
        cur = conn.execute(
            f"""SELECT {_PAGE_SELECT_COLS}
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
        f"""
        SELECT {_PAGE_SELECT_COLS}
        FROM memopedia_pages
        WHERE (title LIKE ? OR summary LIKE ? OR content LIKE ?)
          AND (is_deleted = 0 OR is_deleted IS NULL)
        ORDER BY updated_at DESC
        LIMIT ?
        """,
        (pattern, pattern, pattern, limit),
    )
    return [_row_to_page(row) for row in cur.fetchall()]


def search_pages_filtered(
    conn: sqlite3.Connection,
    query: str,
    *,
    category: Optional[str] = None,
    limit: int = 10,
) -> List[MemopediaPage]:
    """Search non-deleted pages by title/content with optional category filter.

    Args:
        conn: Database connection
        query: Search keyword (LIKE match on title, summary, content)
        category: Optional category filter (CATEGORY_DEFS のキー, e.g. "people")
        limit: Maximum results

    Returns:
        List of matching MemopediaPage, newest first.
    """
    # Split by whitespace and match ANY keyword (OR) across title/summary/content
    keywords = query.split()
    if len(keywords) > 1:
        keyword_conditions = []
        params: list = []
        for kw in keywords:
            pat = f"%{kw}%"
            keyword_conditions.append("(title LIKE ? OR summary LIKE ? OR content LIKE ?)")
            params.extend([pat, pat, pat])
        conditions = [
            f"({' OR '.join(keyword_conditions)})",
            "(is_deleted = 0 OR is_deleted IS NULL)",
        ]
    else:
        pattern = f"%{query}%"
        conditions = [
            "(title LIKE ? OR summary LIKE ? OR content LIKE ?)",
            "(is_deleted = 0 OR is_deleted IS NULL)",
        ]
        params: list = [pattern, pattern, pattern]

    if category:
        conditions.append("category = ?")
        params.append(category)

    where_clause = " AND ".join(conditions)
    params.append(limit)

    cur = conn.execute(
        f"""
        SELECT id, parent_id, title, summary, content, category, created_at, updated_at,
               keywords, vividness, is_trunk, is_important, last_referenced_at, metadata
        FROM memopedia_pages
        WHERE {where_clause}
        ORDER BY updated_at DESC
        LIMIT ?
        """,
        params,
    )
    return [_row_to_page(row) for row in cur.fetchall()]


# ----- Edit history operations -----


def generate_diff(old_content: str, new_content: str, context_lines: int = 3) -> str:
    """Generate a unified diff between old and new content."""
    old_lines = old_content.splitlines(keepends=True)
    new_lines = new_content.splitlines(keepends=True)
    # Ensure last line ends with \n to prevent concatenation in "".join()
    if old_lines and not old_lines[-1].endswith('\n'):
        old_lines[-1] += '\n'
    if new_lines and not new_lines[-1].endswith('\n'):
        new_lines[-1] += '\n'
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
    before_title: Optional[str] = None,
    before_summary: Optional[str] = None,
    before_content: Optional[str] = None,
) -> str:
    """Record an edit history entry for a page.

    The before_* arguments capture the page state immediately before this
    edit. They are used by rollback_page to restore the page reliably. For
    'create' edits the before state is empty strings (the page didn't exist).
    """
    edit_id = str(uuid.uuid4())
    now = int(time.time())
    conn.execute(
        """
        INSERT INTO memopedia_page_edit_history
        (id, page_id, edited_at, diff_text, ref_start_message_id, ref_end_message_id,
         edit_type, edit_source, before_title, before_summary, before_content)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            edit_id, page_id, now, diff_text, ref_start_message_id, ref_end_message_id,
            edit_type, edit_source, before_title, before_summary, before_content,
        ),
    )
    conn.commit()
    return edit_id


_EDIT_HISTORY_COLUMNS = (
    "id, page_id, edited_at, diff_text, ref_start_message_id, ref_end_message_id, "
    "edit_type, edit_source, before_title, before_summary, before_content"
)


def _row_to_edit_history(row) -> PageEditHistory:
    return PageEditHistory(
        id=row[0],
        page_id=row[1],
        edited_at=row[2],
        diff_text=row[3],
        ref_start_message_id=row[4],
        ref_end_message_id=row[5],
        edit_type=row[6],
        edit_source=row[7],
        before_title=row[8],
        before_summary=row[9],
        before_content=row[10],
    )


def get_page_edit_history(
    conn: sqlite3.Connection,
    page_id: str,
    limit: int = 50,
) -> List[PageEditHistory]:
    """Get the edit history for a page, ordered by most recent first."""
    cur = conn.execute(
        f"""
        SELECT {_EDIT_HISTORY_COLUMNS}
        FROM memopedia_page_edit_history
        WHERE page_id = ?
        ORDER BY edited_at DESC
        LIMIT ?
        """,
        (page_id, limit),
    )
    return [_row_to_edit_history(row) for row in cur.fetchall()]


def get_edit_by_id(conn: sqlite3.Connection, edit_id: str) -> Optional[PageEditHistory]:
    """Get a single edit history entry by ID."""
    cur = conn.execute(
        f"""
        SELECT {_EDIT_HISTORY_COLUMNS}
        FROM memopedia_page_edit_history
        WHERE id = ?
        """,
        (edit_id,),
    )
    row = cur.fetchone()
    if row is None:
        return None
    return _row_to_edit_history(row)


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


# ----- Fragment operations -----


def create_fragment(
    conn: sqlite3.Connection,
    *,
    entity_id: str,
    content: str,
    chronicle_entry_id: Optional[str] = None,
    source_date: Optional[str] = None,
) -> MemopediaFragment:
    """Create a new fragment linked to an entity page."""
    frag_id = str(uuid.uuid4())
    now = int(time.time())
    conn.execute(
        """
        INSERT INTO memopedia_fragments (id, content, entity_id, chronicle_entry_id, vividness, source_date, created_at)
        VALUES (?, ?, ?, ?, 'vivid', ?, ?)
        """,
        (frag_id, content, entity_id, chronicle_entry_id, source_date, now),
    )
    conn.commit()
    return MemopediaFragment(
        id=frag_id,
        content=content,
        entity_id=entity_id,
        chronicle_entry_id=chronicle_entry_id,
        vividness="vivid",
        source_date=source_date,
        created_at=now,
    )


def get_fragments_for_entity(
    conn: sqlite3.Connection,
    entity_id: str,
    *,
    vividness_filter: Optional[List[str]] = None,
) -> List[MemopediaFragment]:
    """Get all fragments for an entity, optionally filtered by vividness."""
    if vividness_filter:
        placeholders = ",".join("?" for _ in vividness_filter)
        rows = conn.execute(
            f"SELECT id, content, entity_id, chronicle_entry_id, vividness, source_date, created_at "
            f"FROM memopedia_fragments WHERE entity_id = ? AND vividness IN ({placeholders}) "
            f"ORDER BY created_at",
            [entity_id] + vividness_filter,
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT id, content, entity_id, chronicle_entry_id, vividness, source_date, created_at "
            "FROM memopedia_fragments WHERE entity_id = ? ORDER BY created_at",
            (entity_id,),
        ).fetchall()
    return [
        MemopediaFragment(
            id=r[0], content=r[1], entity_id=r[2],
            chronicle_entry_id=r[3], vividness=r[4],
            source_date=r[5], created_at=r[6],
        )
        for r in rows
    ]

