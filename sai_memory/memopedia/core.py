"""Memopedia core class - high-level API for managing knowledge pages."""

from __future__ import annotations

import logging
import sqlite3
import threading
from typing import Any, Dict, List, Optional

from sai_memory.memopedia.storage import (
    init_memopedia_tables,
    MemopediaPage,
    PageState,
    PageEditHistory,
    CATEGORY_PEOPLE,
    CATEGORY_TERMS,
    CATEGORY_PLANS,
    build_tree,
    create_page,
    delete_page,
    get_page,
    get_children,
    get_open_pages,
    get_all_states_for_thread,
    set_page_open,
    update_page,
    get_last_update_log,
    record_update_log,
    find_page_by_title,
    search_pages,
    generate_diff,
    record_page_edit,
    get_page_edit_history as storage_get_page_edit_history,
    get_edit_by_id,
)

LOGGER = logging.getLogger(__name__)


class Memopedia:
    """High-level interface for Memopedia operations."""

    def __init__(self, conn: sqlite3.Connection, *, db_lock: Optional[threading.RLock] = None):
        """
        Initialize Memopedia with a database connection.

        Args:
            conn: SQLite connection (should be the same as SAIMemory's connection)
            db_lock: Optional lock for thread-safe operations (share with SAIMemoryAdapter)
        """
        self.conn = conn
        self._lock = db_lock or threading.RLock()

        # Initialize tables
        with self._lock:
            init_memopedia_tables(conn)

        LOGGER.info("Memopedia initialized")

    # ----- Tree operations -----

    def get_tree(self, thread_id: Optional[str] = None) -> Dict[str, Any]:
        """
        Get the full page tree with optional open/close states.

        Args:
            thread_id: If provided, includes is_open state for each page

        Returns:
            {
                "people": [{"id": ..., "title": ..., "summary": ..., "is_open": bool, "children": [...]}],
                "events": [...],
                "plans": [...]
            }
        """
        with self._lock:
            tree = build_tree(self.conn)
            states = get_all_states_for_thread(self.conn, thread_id) if thread_id else {}

        def _annotate(page: MemopediaPage) -> Dict[str, Any]:
            result = {
                "id": page.id,
                "title": page.title,
                "summary": page.summary,
                "keywords": page.keywords,
                "vividness": page.vividness,
                "content": page.content,  # Include content for vivid pages
                "is_open": states.get(page.id, False),
                "children": [_annotate(c) for c in page.children],
            }
            return result

        return {
            "people": [_annotate(p) for p in tree.get(CATEGORY_PEOPLE, [])],
            "terms": [_annotate(p) for p in tree.get(CATEGORY_TERMS, [])],
            "plans": [_annotate(p) for p in tree.get(CATEGORY_PLANS, [])],
        }

    def get_tree_markdown(self, thread_id: Optional[str] = None) -> str:
        """
        Get the page tree as a Markdown outline.

        Open pages are marked with [OPEN], closed with [-].
        """
        tree = self.get_tree(thread_id)
        lines: List[str] = []

        category_names = {
            "people": "人物",
            "terms": "用語",
            "plans": "予定",
        }

        def _render_page(page: Dict[str, Any], depth: int = 0) -> None:
            indent = "  " * depth
            marker = "[OPEN]" if page.get("is_open") else "[-]"
            summary = page.get("summary", "")
            summary_part = f" - {summary}" if summary else ""
            lines.append(f"{indent}- {marker} **{page['title']}**{summary_part}")
            for child in page.get("children", []):
                _render_page(child, depth + 1)

        for category_key in ["people", "terms", "plans"]:
            category_name = category_names.get(category_key, category_key)
            pages = tree.get(category_key, [])
            lines.append(f"\n## {category_name}\n")
            for page in pages:
                _render_page(page, depth=0)

        return "\n".join(lines)

    # ----- Page operations -----

    def get_page(self, page_id: str) -> Optional[MemopediaPage]:
        """Get a page by ID."""
        with self._lock:
            return get_page(self.conn, page_id)

    def get_page_full(self, page_id: str) -> Optional[Dict[str, Any]]:
        """Get a page with full details including children list."""
        with self._lock:
            page = get_page(self.conn, page_id)
            if page is None:
                return None
            children = get_children(self.conn, page_id)
            return {
                "id": page.id,
                "parent_id": page.parent_id,
                "title": page.title,
                "summary": page.summary,
                "content": page.content,
                "category": page.category,
                "created_at": page.created_at,
                "updated_at": page.updated_at,
                "children": [{"id": c.id, "title": c.title, "summary": c.summary} for c in children],
            }

    def create_page(
        self,
        *,
        parent_id: str,
        title: str,
        summary: str = "",
        content: str = "",
        keywords: Optional[List[str]] = None,
        vividness: str = "rough",
        ref_start_message_id: Optional[str] = None,
        ref_end_message_id: Optional[str] = None,
        edit_source: Optional[str] = None,
    ) -> MemopediaPage:
        """
        Create a new page under an existing parent.

        The category is inherited from the parent page.

        Args:
            parent_id: ID of the parent page
            title: Page title
            summary: Page summary
            content: Page content
            keywords: List of keywords
            vividness: Vividness level (vivid/rough/faint/buried), default: rough
            ref_start_message_id: Start of message reference range
            ref_end_message_id: End of message reference range
            edit_source: Source of this edit (e.g., 'ai_conversation', 'manual')
        """
        with self._lock:
            parent = get_page(self.conn, parent_id)
            if parent is None:
                raise ValueError(f"Parent page not found: {parent_id}")
            page = create_page(
                self.conn,
                parent_id=parent_id,
                title=title,
                summary=summary,
                content=content,
                category=parent.category,
                keywords=keywords,
                vividness=vividness,
            )
            # Record edit history for create
            full_content = f"title: {title}\nsummary: {summary}\ncontent:\n{content}"
            diff_text = generate_diff("", full_content)
            record_page_edit(
                self.conn,
                page_id=page.id,
                diff_text=diff_text,
                edit_type="create",
                ref_start_message_id=ref_start_message_id,
                ref_end_message_id=ref_end_message_id,
                edit_source=edit_source,
            )
            return page

    def update_page(
        self,
        page_id: str,
        *,
        title: Optional[str] = None,
        summary: Optional[str] = None,
        content: Optional[str] = None,
        keywords: Optional[List[str]] = None,
        vividness: Optional[str] = None,
        ref_start_message_id: Optional[str] = None,
        ref_end_message_id: Optional[str] = None,
        edit_source: Optional[str] = None,
    ) -> Optional[MemopediaPage]:
        """Update a page's title, summary, content, keywords, or vividness."""
        with self._lock:
            # Get old page for diff
            old_page = get_page(self.conn, page_id)
            if old_page is None:
                return None
            old_content = f"title: {old_page.title}\nsummary: {old_page.summary}\ncontent:\n{old_page.content}"

            result = update_page(
                self.conn,
                page_id,
                title=title,
                summary=summary,
                content=content,
                keywords=keywords,
                vividness=vividness,
            )

            if result:
                new_content = f"title: {result.title}\nsummary: {result.summary}\ncontent:\n{result.content}"
                diff_text = generate_diff(old_content, new_content)
                if diff_text:  # Only record if there's an actual change
                    record_page_edit(
                        self.conn,
                        page_id=page_id,
                        diff_text=diff_text,
                        edit_type="update",
                        ref_start_message_id=ref_start_message_id,
                        ref_end_message_id=ref_end_message_id,
                        edit_source=edit_source,
                    )
            return result

    def append_to_content(
        self,
        page_id: str,
        text: str,
        ref_start_message_id: Optional[str] = None,
        ref_end_message_id: Optional[str] = None,
        edit_source: Optional[str] = None,
    ) -> Optional[MemopediaPage]:
        """Append text to a page's content."""
        with self._lock:
            page = get_page(self.conn, page_id)
            if page is None:
                return None
            old_content = page.content
            new_content = page.content + "\n\n" + text if page.content else text
            result = update_page(self.conn, page_id, content=new_content)

            if result:
                diff_text = generate_diff(old_content, new_content)
                record_page_edit(
                    self.conn,
                    page_id=page_id,
                    diff_text=diff_text,
                    edit_type="append",
                    ref_start_message_id=ref_start_message_id,
                    ref_end_message_id=ref_end_message_id,
                    edit_source=edit_source,
                )
            return result

    def delete_page(
        self,
        page_id: str,
        ref_start_message_id: Optional[str] = None,
        ref_end_message_id: Optional[str] = None,
        edit_source: Optional[str] = None,
    ) -> bool:
        """
        Soft-delete a page (mark as deleted but keep in DB).

        The page and its edit history are preserved for reference.
        """
        # Prevent deleting root pages
        if page_id.startswith("root_"):
            LOGGER.warning("Cannot delete root page: %s", page_id)
            return False
        with self._lock:
            page = get_page(self.conn, page_id)
            if page is None:
                return False

            # Record delete in edit history
            full_content = f"title: {page.title}\nsummary: {page.summary}\ncontent:\n{page.content}"
            diff_text = generate_diff(full_content, "")
            record_page_edit(
                self.conn,
                page_id=page_id,
                diff_text=diff_text,
                edit_type="delete",
                ref_start_message_id=ref_start_message_id,
                ref_end_message_id=ref_end_message_id,
                edit_source=edit_source,
            )

            # Soft delete: mark as deleted instead of removing
            self.conn.execute(
                "UPDATE memopedia_pages SET is_deleted = 1 WHERE id = ?",
                (page_id,),
            )
            self.conn.commit()
            return True

    def find_by_title(self, title: str, category: Optional[str] = None) -> Optional[MemopediaPage]:
        """Find a page by exact title match."""
        with self._lock:
            return find_page_by_title(self.conn, title, category)

    def search(self, query: str, limit: int = 10) -> List[MemopediaPage]:
        """Search pages by title, summary, or content."""
        with self._lock:
            return search_pages(self.conn, query, limit)

    # ----- Edit history operations -----

    def get_page_edit_history(self, page_id: str, limit: int = 50) -> List[PageEditHistory]:
        """
        Get the edit history for a page.

        Returns list of edits ordered by most recent first.
        Each entry contains the diff, reference message range, and edit source.
        """
        with self._lock:
            return storage_get_page_edit_history(self.conn, page_id, limit)

    # ----- Page state operations (for thread/session) -----

    def open_page(self, thread_id: str, page_id: str) -> Dict[str, Any]:
        """
        Open a page for a thread, returning its full content.

        Returns:
            {"title": ..., "summary": ..., "content": ..., "children": [...]}
        """
        with self._lock:
            set_page_open(self.conn, thread_id, page_id, True)
            page = get_page(self.conn, page_id)
            if page is None:
                return {"error": f"Page not found: {page_id}"}
            children = get_children(self.conn, page_id)
            return {
                "title": page.title,
                "summary": page.summary,
                "content": page.content,
                "children": [{"id": c.id, "title": c.title, "summary": c.summary} for c in children],
            }

    def close_page(self, thread_id: str, page_id: str) -> Dict[str, Any]:
        """Close a page for a thread."""
        with self._lock:
            set_page_open(self.conn, thread_id, page_id, False)
            return {"success": True, "page_id": page_id}

    def get_open_pages(self, thread_id: str) -> List[MemopediaPage]:
        """Get all pages currently open for a thread."""
        with self._lock:
            return get_open_pages(self.conn, thread_id)

    def get_open_pages_content(self, thread_id: str) -> str:
        """
        Get the content of all open pages as Markdown.

        This is what gets injected into the persona's context.
        """
        pages = self.get_open_pages(thread_id)
        if not pages:
            return ""

        sections: List[str] = []
        for page in pages:
            section_lines = [f"## {page.title}"]
            if page.summary:
                section_lines.append(f"*{page.summary}*")
            if page.content:
                section_lines.append("")
                section_lines.append(page.content)
            sections.append("\n".join(section_lines))

        return "\n\n---\n\n".join(sections)

    # ----- Update tracking -----

    def get_last_update(self) -> Optional[Dict[str, Any]]:
        """Get the last update log entry."""
        with self._lock:
            return get_last_update_log(self.conn)

    def record_update(
        self,
        *,
        last_message_id: Optional[str],
        last_message_created_at: Optional[int],
    ) -> str:
        """Record that an update was processed."""
        with self._lock:
            return record_update_log(
                self.conn,
                last_message_id=last_message_id,
                last_message_created_at=last_message_created_at,
            )

    # ----- Utility -----

    def get_page_markdown(self, page_id: str) -> str:
        """Get a single page as Markdown."""
        page = self.get_page(page_id)
        if page is None:
            return ""

        lines = [f"# {page.title}"]
        if page.summary:
            lines.append(f"\n*{page.summary}*")
        if page.content:
            lines.append(f"\n{page.content}")

        with self._lock:
            children = get_children(self.conn, page_id)
        if children:
            lines.append("\n## 子ページ")
            for child in children:
                lines.append(f"- **{child.title}**: {child.summary}")

        return "\n".join(lines)

    def export_all_markdown(self) -> str:
        """Export all pages as a single Markdown document."""
        with self._lock:
            tree = build_tree(self.conn)

        category_names = {
            CATEGORY_PEOPLE: "人物",
            CATEGORY_TERMS: "用語",
            CATEGORY_PLANS: "予定",
        }

        sections: List[str] = ["# Memopedia\n"]

        def _render_page(page: MemopediaPage, level: int = 2) -> List[str]:
            lines = []
            heading = "#" * min(level, 6)
            lines.append(f"{heading} {page.title}")
            if page.summary:
                lines.append(f"\n*{page.summary}*")
            if page.content:
                lines.append(f"\n{page.content}")
            for child in page.children:
                lines.append("")
                lines.extend(_render_page(child, level + 1))
            return lines

        for category in [CATEGORY_PEOPLE, CATEGORY_TERMS, CATEGORY_PLANS]:
            category_name = category_names.get(category, category)
            pages = tree.get(category, [])
            if not pages:
                continue
            sections.append(f"# {category_name}\n")
            for page in pages:
                # Skip root pages' own title since category heading is enough
                if page.id.startswith("root_"):
                    for child in page.children:
                        sections.extend(_render_page(child, level=2))
                else:
                    sections.extend(_render_page(page, level=2))
            sections.append("")

        return "\n".join(sections)

    # ----- JSON Export/Import -----

    def export_json(self) -> Dict[str, Any]:
        """
        Export all pages as a JSON-serializable dict.

        Returns:
            {
                "version": 1,
                "pages": [
                    {
                        "id": "...",
                        "parent_id": "...",
                        "title": "...",
                        "summary": "...",
                        "content": "...",
                        "category": "...",
                        "created_at": ...,
                        "updated_at": ...
                    },
                    ...
                ]
            }
        """
        with self._lock:
            from sai_memory.memopedia.storage import get_all_pages
            all_pages = get_all_pages(self.conn)

        pages_data = []
        for page in all_pages:
            # Skip root pages (they're auto-created on init)
            if page.id.startswith("root_"):
                continue
            pages_data.append({
                "id": page.id,
                "parent_id": page.parent_id,
                "title": page.title,
                "summary": page.summary,
                "content": page.content,
                "category": page.category,
                "created_at": page.created_at,
                "updated_at": page.updated_at,
            })

        return {
            "version": 1,
            "pages": pages_data,
        }

    def import_json(self, data: Dict[str, Any], *, clear_existing: bool = False) -> int:
        """
        Import pages from a JSON dict.

        Args:
            data: JSON data from export_json()
            clear_existing: If True, delete all non-root pages before importing

        Returns:
            Number of pages imported
        """
        version = data.get("version", 1)
        pages_data = data.get("pages", [])

        if not pages_data:
            LOGGER.warning("No pages to import")
            return 0

        with self._lock:
            if clear_existing:
                # Delete all non-root pages
                from sai_memory.memopedia.storage import get_all_pages, delete_page
                existing = get_all_pages(self.conn)
                for page in existing:
                    if not page.id.startswith("root_"):
                        delete_page(self.conn, page.id)
                LOGGER.info("Cleared existing pages")

            # Import pages - need to handle parent relationships
            # Sort by parent_id to ensure parents are created first
            # Root pages (parent_id starting with "root_") should come first
            def sort_key(p):
                parent = p.get("parent_id", "")
                if parent and parent.startswith("root_"):
                    return (0, parent)
                elif not parent:
                    return (1, "")
                else:
                    return (2, parent)

            sorted_pages = sorted(pages_data, key=sort_key)

            from sai_memory.memopedia.storage import create_page, get_page
            imported = 0

            for page_data in sorted_pages:
                page_id = page_data.get("id")
                parent_id = page_data.get("parent_id")
                title = page_data.get("title", "")
                summary = page_data.get("summary", "")
                content = page_data.get("content", "")
                category = page_data.get("category", "")

                # Skip if page already exists
                if get_page(self.conn, page_id):
                    LOGGER.debug("Page %s already exists, skipping", page_id)
                    continue

                try:
                    create_page(
                        self.conn,
                        parent_id=parent_id,
                        title=title,
                        summary=summary,
                        content=content,
                        category=category,
                        page_id=page_id,
                    )
                    imported += 1
                    LOGGER.debug("Imported page: %s", title)
                except Exception as e:
                    LOGGER.warning("Failed to import page %s: %s", title, e)

            LOGGER.info("Imported %d pages", imported)
            return imported

    def clear_all_pages(self) -> int:
        """
        Delete all non-root pages.

        Returns:
            Number of pages deleted
        """
        with self._lock:
            from sai_memory.memopedia.storage import get_all_pages, delete_page
            existing = get_all_pages(self.conn)
            deleted = 0
            for page in existing:
                if not page.id.startswith("root_"):
                    delete_page(self.conn, page.id)
                    deleted += 1
            LOGGER.info("Deleted %d pages", deleted)
            return deleted
