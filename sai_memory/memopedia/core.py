"""Memopedia core class - high-level API for managing knowledge pages."""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterator, List, Optional, Sequence

from sai_memory.memopedia.storage import (
    init_memopedia_tables,
    MemopediaPage,
    PageState,
    PageEditHistory,
    MemopediaFragment,
    CATEGORY_PEOPLE,
    CATEGORY_TERMS,
    CATEGORY_PLANS,
    CATEGORY_EVENTS,
    CATEGORY_THEME,
    CATEGORY_DEFS,
    category_keys,
    category_label,
    build_tree,
    create_page,
    get_page,
    resolve_page_ref,
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
    # Trunk operations
    set_trunk_flag,
    get_trunks as storage_get_trunks,
    move_pages_to_parent,
    get_unorganized_pages as storage_get_unorganized_pages,
    # Important flag
    set_important_flag,
    # Fragment operations
    create_fragment as storage_create_fragment,
    fragment_exists,
    get_fragments_for_entity,
)

LOGGER = logging.getLogger(__name__)


class ChronicleProtectedError(RuntimeError):
    """Chronicle エントリ (時間の地図ページ) への可視性操作の拒否。

    soft delete (is_deleted=1) や trunk 化 (is_trunk=1) は互換ビュー
    ``arasuji_entries`` から entry を消すのに、知覚バッチの付記印
    (annexed_entry_id) を戻す仕組みを持たない — 「付記済み = 提示に出ない」の
    まま転写先も不可視という知覚の恒久消失を作る (2026-08-19 Codex 第三巡 #1)。
    Chronicle の削除は専用経路 (sai_memory/arasuji/storage.py の delete_entry 系
    — 付記印の返却を同一 tx で行う) だけを許可する。

    False/None で返すと呼び出し側が「未発見」と区別できない (同 第四巡 #1) ので
    専用例外で表明する (流儀は ChunkExecutionError / BackupError と同じ
    RuntimeError 派生)。
    """

    def __init__(self, page_id: str, operation: str) -> None:
        self.page_id = page_id
        self.operation = operation
        super().__init__(
            f"chronicle page {page_id} is protected from {operation}; "
            "use the arasuji deletion APIs instead"
        )


@dataclass
class EntityNotes:
    """1 エンティティぶんの適用内容 (:meth:`Memopedia.apply_entity_notes` の入力)。

    抽出器 (``entity_extractor``) が LLM の出力から組み立てて渡す。Memopedia は
    「誰がどうやって作ったか」を知らずに、この形だけを受け取って適用する。
    """

    title: str
    #: 新規作成するときの置き場所 (カテゴリのルートページ id)
    parent_id: str
    summary: str = ""
    notes: List[str] = field(default_factory=list)


@dataclass
class EntityApplyResult:
    """:meth:`Memopedia.apply_entity_notes` の適用結果 (1 エンティティぶん)。"""

    title: str
    page_id: str
    is_new_page: bool
    #: 新しく作った Fragment の数
    fragments_created: int = 0
    #: 同じ出所・同じ文が既にあったので作らなかった数 (拾い直しの二度目)
    fragments_deduped: int = 0


class Memopedia:
    """High-level interface for Memopedia operations."""

    def __init__(self, conn: sqlite3.Connection, *, db_lock: Optional[threading.RLock] = None):
        """
        Initialize Memopedia with a database connection.

        Args:
            conn: SQLite connection (should be the same as SAIMemory's connection)
            db_lock: 錠前。**省いてよい** —— 省くと接続が指している DB ファイルの
                錠前を配り所 (:func:`sai_memory.db_locks.lock_for`) から取る。
                同じ DB を開いた書き手は、渡されなくても同じ錠前を持つ。

                以前はここで新しい ``RLock`` を作っていた。渡し忘れると「守って
                いるつもりで排他が成立しない」状態になり、記憶の追記が黙って
                落ちた (docs/issues/memopedia_writers_bypass_adapter_lock.md、
                まはー裁定 2026-08-06)。
        """
        from sai_memory.db_locks import lock_for

        self.conn = conn
        self._lock = db_lock if db_lock is not None else lock_for(conn)

        # Initialize tables
        with self._lock:
            init_memopedia_tables(conn)

        LOGGER.info("Memopedia initialized")

    @contextmanager
    def _atomic(self) -> Iterator[sqlite3.Connection]:
        """複数の書き込みを「全部入るか、何も入らないか」に束ねる。

        ロックを保持したまま ``BEGIN IMMEDIATE`` で書き込みロックを先に取り、
        中の storage 関数は ``commit=False`` で呼ぶ。途中で落ちたら rollback —
        ページだけ・Fragment だけが残る部分適用を作らない (部分適用は、拾い直し
        が同じ知識を新しい UUID で二重に挿す道になる)。

        既に呼び出し元がトランザクションを開いていればそれに参加し、確定
        (commit / rollback) はその呼び出し元に委ねる —— 他人の書き込みを
        巻き込んで確定しない。
        """
        with self._lock:
            if self.conn.in_transaction:
                yield self.conn
                return
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                yield self.conn
                self.conn.commit()
            except Exception:
                try:
                    self.conn.rollback()
                except Exception:
                    LOGGER.warning("Memopedia rollback failed", exc_info=True)
                raise

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
                "short_id": page.short_id,
                "title": page.title,
                "summary": page.summary,
                "keywords": page.keywords,
                # P4-c: vividness は廃止 (死置きカラム)。get_tree レスポンスから除外。
                "is_trunk": page.is_trunk,
                "is_important": page.is_important,
                "content": page.content,
                "is_open": states.get(page.id, False),
                "updated_at": page.updated_at,
                "last_referenced_at": page.last_referenced_at,
                "children": [_annotate(c) for c in page.children],
            }
            return result

        return {
            key: [_annotate(p) for p in tree.get(key, [])]
            for key in category_keys("in_tree")
        }

    def get_tree_markdown(
        self,
        thread_id: Optional[str] = None,
        include_keywords: bool = False,
        max_depth: Optional[int] = None,
        show_markers: bool = True,
        include_summary: bool = True,
    ) -> str:
        """
        Get the page tree as a Markdown outline.

        This is the unified method for formatting Memopedia content for LLM contexts.

        Args:
            thread_id: Optional thread ID to include open/close states
            include_keywords: If True, include keywords in output (default: False for lighter context)
            max_depth: Maximum tree depth to include (None = unlimited, 0 = root only, 1 = root + children, etc.)
            show_markers: If True, show [OPEN]/[-] markers (default: True for chat, False for analysis scripts)
            include_summary: If False, omit page summaries (default: True). P4-d head 目次は
                summary を省いて title + markers + count のみを出す。

        Returns:
            Formatted Markdown string of the page tree
        """
        tree = self.get_tree(thread_id)
        lines: List[str] = []

        def _render_page(page: Dict[str, Any], depth: int = 0, current_depth: int = 0) -> None:
            # Check depth limit
            if max_depth is not None and current_depth > max_depth:
                return

            # Skip root pages
            if page.get("id", "").startswith("root_"):
                # Still process children of root pages
                for child in page.get("children", []):
                    _render_page(child, depth, current_depth)
                return

            indent = "  " * depth

            # Build line content
            sid = page.get("short_id")
            id_suffix = f" [id: memopedia:{sid}]" if sid else ""
            if show_markers:
                marker = "[OPEN]" if page.get("is_open") else "[-]"
                title_part = f"{marker} **{page['title']}**{id_suffix}"
            else:
                title_part = f"{page['title']}{id_suffix}"

            # important flag (★)
            if page.get("is_important"):
                title_part += " ★"

            summary_part = ""
            if include_summary:
                summary = page.get("summary", "")
                summary_part = f": {summary}" if summary else ""

            # Add keywords if enabled
            if include_keywords:
                keywords = page.get("keywords", [])
                if keywords:
                    kw_str = f" [キーワード: {', '.join(keywords)}]"
                    summary_part += kw_str

            lines.append(f"{indent}- {title_part}{summary_part}")

            # Process children (if within depth limit)
            children = page.get("children", [])
            if children and (max_depth is None or current_depth + 1 <= max_depth):
                for child in children:
                    _render_page(child, depth + 1, current_depth + 1)

        for category_key in category_keys("in_tree"):
            category_name = category_label(category_key)
            pages = tree.get(category_key, [])
            if pages:
                lines.append(f"\n### {category_name}")
                for page in pages:
                    _render_page(page, depth=0, current_depth=0)

        if not lines:
            return "(まだページはありません)"

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
        is_trunk: bool = False,
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
            is_trunk: If True, this page is a trunk (category container)
            ref_start_message_id: Start of message reference range
            ref_end_message_id: End of message reference range
            edit_source: Source of this edit (e.g., 'ai_conversation', 'manual')
        """
        with self._atomic() as conn:
            return self._create_page_in_tx(
                conn,
                parent_id=parent_id,
                title=title,
                summary=summary,
                content=content,
                keywords=keywords,
                vividness=vividness,
                is_trunk=is_trunk,
                ref_start_message_id=ref_start_message_id,
                ref_end_message_id=ref_end_message_id,
                edit_source=edit_source,
            )

    def _create_page_in_tx(
        self,
        conn: sqlite3.Connection,
        *,
        parent_id: str,
        title: str,
        summary: str = "",
        content: str = "",
        keywords: Optional[List[str]] = None,
        vividness: str = "rough",
        is_trunk: bool = False,
        ref_start_message_id: Optional[str] = None,
        ref_end_message_id: Optional[str] = None,
        edit_source: Optional[str] = None,
    ) -> MemopediaPage:
        """ページ作成の本体 (ロックとトランザクションは呼び出し側が持つ)。"""
        parent = get_page(conn, parent_id)
        if parent is None:
            raise ValueError(f"Parent page not found: {parent_id}")
        page = create_page(
            conn,
            parent_id=parent_id,
            title=title,
            summary=summary,
            content=content,
            category=parent.category,
            keywords=keywords,
            vividness=vividness,
            is_trunk=is_trunk,
            commit=False,
        )
        # Record edit history for create. Before-state is empty since the
        # page didn't exist; rollback to before a 'create' edit is treated
        # as effectively undoing the page (caller's responsibility).
        full_content = f"title: {title}\nsummary: {summary}\ncontent:\n{content}"
        diff_text = generate_diff("", full_content)
        record_page_edit(
            conn,
            page_id=page.id,
            diff_text=diff_text,
            edit_type="create",
            ref_start_message_id=ref_start_message_id,
            ref_end_message_id=ref_end_message_id,
            edit_source=edit_source,
            before_title="",
            before_summary="",
            before_content="",
            commit=False,
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
        with self._atomic() as conn:
            return self._update_page_in_tx(
                conn,
                page_id,
                title=title,
                summary=summary,
                content=content,
                keywords=keywords,
                vividness=vividness,
                ref_start_message_id=ref_start_message_id,
                ref_end_message_id=ref_end_message_id,
                edit_source=edit_source,
            )

    def _update_page_in_tx(
        self,
        conn: sqlite3.Connection,
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
        """ページ更新の本体 (ロックとトランザクションは呼び出し側が持つ)。"""
        # Get old page for diff
        old_page = get_page(conn, page_id)
        if old_page is None:
            return None
        old_content = f"title: {old_page.title}\nsummary: {old_page.summary}\ncontent:\n{old_page.content}"

        result = update_page(
            conn,
            page_id,
            title=title,
            summary=summary,
            content=content,
            keywords=keywords,
            vividness=vividness,
            commit=False,
        )

        if result:
            new_content = f"title: {result.title}\nsummary: {result.summary}\ncontent:\n{result.content}"
            diff_text = generate_diff(old_content, new_content)
            if diff_text:  # Only record if there's an actual change
                record_page_edit(
                    conn,
                    page_id=page_id,
                    diff_text=diff_text,
                    edit_type="update",
                    ref_start_message_id=ref_start_message_id,
                    ref_end_message_id=ref_end_message_id,
                    edit_source=edit_source,
                    before_title=old_page.title,
                    before_summary=old_page.summary,
                    before_content=old_page.content,
                    commit=False,
                )
            # Update reference timestamp (同じ tx の中で — 更新と参照時刻が
            # 別々に確定すると、片方だけ残る中途半端な状態ができる)
            self._touch_page_in_tx(conn, page_id)
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
                    before_title=page.title,
                    before_summary=page.summary,
                    before_content=old_content,
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
            # Chronicle エントリはここでは消さない — 理由と経路は
            # ChronicleProtectedError の docstring。「未発見の False」と区別
            # できるよう専用例外で表明する。
            if page.category == "chronicle":
                raise ChronicleProtectedError(page_id, "soft delete")

            # Record delete in edit history. Before-state captures the page
            # as it existed prior to deletion so rollback can restore it.
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
                before_title=page.title,
                before_summary=page.summary,
                before_content=page.content,
            )

            # Soft delete: mark as deleted and bump updated_at so external
            # observers (e.g. dynamic_state diff) can detect the deletion via timestamp.
            now = int(time.time())
            self.conn.execute(
                "UPDATE memopedia_pages SET is_deleted = 1, updated_at = ? WHERE id = ?",
                (now, page_id),
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

    def rollback_page(self, page_id: str, to_edit_id: str) -> Optional[MemopediaPage]:
        """Rollback a page to the state BEFORE a specific edit.

        Restores the page from the before-snapshot stored on the target edit
        history entry. Edits recorded before v0.3.0 do not carry a snapshot
        (before_* columns are NULL) and cannot be rolled back — None is
        returned in that case.

        Args:
            page_id: Page to rollback.
            to_edit_id: The edit to undo. The state BEFORE this edit is restored.

        Returns:
            Updated MemopediaPage, or None on failure (page or edit missing,
            mismatched edit, or legacy entry without snapshot).
        """
        LOGGER.info("[rollback] Starting rollback for page=%s to_edit=%s", page_id, to_edit_id)
        with self._lock:
            page = get_page(self.conn, page_id)
            if page is None:
                LOGGER.warning("[rollback] Page not found: %s", page_id)
                return None

            target_edit = get_edit_by_id(self.conn, to_edit_id)
            if target_edit is None:
                LOGGER.warning("[rollback] Edit %s not found", to_edit_id)
                return None
            if target_edit.page_id != page_id:
                LOGGER.warning(
                    "[rollback] Edit %s belongs to page %s, not %s",
                    to_edit_id, target_edit.page_id, page_id,
                )
                return None

            if (
                target_edit.before_title is None
                and target_edit.before_summary is None
                and target_edit.before_content is None
            ):
                LOGGER.warning(
                    "[rollback] Edit %s has no before-snapshot (recorded before v0.3.0). "
                    "Rollback is not supported for legacy entries.",
                    to_edit_id,
                )
                return None

            restored_title = target_edit.before_title or ""
            restored_summary = target_edit.before_summary or ""
            restored_content = target_edit.before_content or ""

            if (
                restored_title == page.title
                and restored_summary == page.summary
                and restored_content == page.content
            ):
                LOGGER.info("[rollback] Page already matches before-snapshot, nothing to do")
                return page

            result = update_page(
                self.conn, page_id,
                title=restored_title, summary=restored_summary, content=restored_content,
            )

            if result:
                old_text = f"title: {page.title}\nsummary: {page.summary}\ncontent:\n{page.content}"
                new_text = f"title: {result.title}\nsummary: {result.summary}\ncontent:\n{result.content}"
                diff_text = generate_diff(old_text, new_text)
                if diff_text:
                    record_page_edit(
                        self.conn,
                        page_id=page_id,
                        diff_text=diff_text,
                        edit_type="rollback",
                        edit_source=f"rollback_to_before_{to_edit_id[:8]}",
                        before_title=page.title,
                        before_summary=page.summary,
                        before_content=page.content,
                    )

            return result

    # ----- Page state operations (for thread/session) -----

    def open_page(self, thread_id: str, page_id: str) -> Dict[str, Any]:
        """
        Open a page for a thread, returning its full content.

        Returns:
            {"title": ..., "summary": ..., "content": ..., "children": [...]}
        """
        with self._lock:
            resolved = resolve_page_ref(self.conn, page_id)
            if resolved is None:
                return {"error": f"Page not found: {page_id}"}
            page_id = resolved
            set_page_open(self.conn, thread_id, page_id, True)
            page = get_page(self.conn, page_id)
            if page is None:
                return {"error": f"Page not found: {page_id}"}
            children = get_children(self.conn, page_id)

        # Update reference timestamp (outside lock to avoid deadlock)
        self.touch_page(page_id)

        # P4-c: vividness 廃止のため buried/faint 昇格は除去。

        return {
            "title": page.title,
            "summary": page.summary,
            "content": page.content,
            "children": [{"id": c.id, "title": c.title, "summary": c.summary} for c in children],
        }

    def close_page(self, thread_id: str, page_id: str) -> Dict[str, Any]:
        """Close a page for a thread."""
        with self._lock:
            resolved = resolve_page_ref(self.conn, page_id)
            if resolved is None:
                return {"error": f"Page not found: {page_id}"}
            page_id = resolved
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
        Includes both manual content and fragments.
        """
        pages = self.get_open_pages(thread_id)
        if not pages:
            return ""

        sections: List[str] = []
        for page in pages:
            section_lines = [f"## {page.title}"]
            if page.summary:
                section_lines.append(f"*{page.summary}*")
            body = self.render_page_body(page.id)
            if body:
                section_lines.append("")
                section_lines.append(body)
            sections.append("\n".join(section_lines))

        return "\n\n---\n\n".join(sections)

    # ----- Reference tracking (vividness management) -----

    def touch_page(self, page_id: str) -> None:
        """Update last_referenced_at timestamp for a page.

        Called automatically when a page is opened or updated,
        used by apply_vividness_decay() to determine decay timing.
        """
        with self._atomic() as conn:
            self._touch_page_in_tx(conn, page_id)

    @staticmethod
    def _touch_page_in_tx(conn: sqlite3.Connection, page_id: str) -> None:
        """参照時刻の更新 (ロックとトランザクションは呼び出し側が持つ)。"""
        conn.execute(
            "UPDATE memopedia_pages SET last_referenced_at = ? WHERE id = ?",
            (int(time.time()), page_id),
        )

    def apply_vividness_decay(self) -> int:
        """Apply time-based vividness decay to all non-root pages.

        Decay rules:
        - vivid → rough after 14 days without reference
        - rough → faint after 30 days without reference (skipped for important pages)
        - faint → buried after 60 days without reference (skipped for important pages)

        Important pages (is_important=1) will never decay below 'rough'.

        Returns:
            Number of pages whose vividness was changed
        """
        import time as _time

        now = int(_time.time())
        # (from_level, to_level, threshold_secs, skip_important)
        decay_rules = [
            ("vivid", "rough", 14 * 86400, False),
            ("rough", "faint", 30 * 86400, True),
            ("faint", "buried", 60 * 86400, True),
        ]

        changed = 0
        with self._lock:
            for from_level, to_level, threshold_secs, skip_important in decay_rules:
                cutoff = now - threshold_secs
                important_clause = "AND (is_important = 0 OR is_important IS NULL)" if skip_important else ""
                cur = self.conn.execute(
                    f"""
                    UPDATE memopedia_pages
                    SET vividness = ?, updated_at = ?
                    WHERE vividness = ?
                      AND (last_referenced_at IS NOT NULL AND last_referenced_at < ?)
                      AND id NOT LIKE 'root_%'
                      AND (is_deleted = 0 OR is_deleted IS NULL)
                      {important_clause}
                    """,
                    (to_level, now, from_level, cutoff),
                )
                count = cur.rowcount
                if count > 0:
                    LOGGER.info(
                        "Vividness decay: %d pages %s → %s",
                        count, from_level, to_level,
                    )
                    changed += count
            if changed > 0:
                self.conn.commit()
        return changed

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

        for category in category_keys("in_tree"):
            category_name = category_label(category)
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
            # Chronicle エントリは export しない — level / source_ids / short_id
            # が metadata JSON にあり、この形式では運ばれない (import しても
            # 壊れた entry しか復元できない)。Chronicle の持ち出しは編纂系の
            # 専用経路の領分 (2026-08-19 Codex 第五巡 #1)。
            if page.category == "chronicle":
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
        pages_data = data.get("pages", [])

        if not pages_data:
            LOGGER.warning("No pages to import")
            return 0

        with self._lock:
            if clear_existing:
                # Delete all non-root pages。Chronicle エントリは対象外 —
                # Memopedia の一括操作が時間の地図を物理削除してはいけない
                # (一括削除の専用経路は arasuji 側の clear_all_entries だけ。
                # 2026-08-19 Codex 第五巡 #1 — W14 以前からの既存欠陥)。
                from sai_memory.memopedia.storage import get_all_pages, delete_page
                existing = get_all_pages(self.conn)
                for page in existing:
                    if page.id.startswith("root_") or page.category == "chronicle":
                        continue
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

                # Chronicle エントリは import でも作らない — この形式は
                # metadata (level / source_ids) を運ばず、壊れた entry しか
                # 生めない (export 側でも除外済み。旧 export の残骸対策)。
                if category == "chronicle":
                    LOGGER.warning(
                        "Skipping chronicle page %s in import (chronicle is "
                        "managed by the arasuji pipeline)", page_id,
                    )
                    continue

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
                # Chronicle は対象外 (専用の一括削除 = arasuji clear_all_entries。
                # 2026-08-19 Codex 第五巡 #1 — W14 以前からの既存欠陥)。
                if page.id.startswith("root_") or page.category == "chronicle":
                    continue
                delete_page(self.conn, page.id)
                deleted += 1
            LOGGER.info("Deleted %d pages", deleted)
            return deleted

    # ----- Trunk operations -----

    def set_trunk(self, page_id: str, is_trunk: bool) -> Optional[MemopediaPage]:
        """
        Set or unset the trunk flag for a page.

        A trunk is a category container page that can hold other pages.
        Trunks are displayed differently in the UI and used for organization.

        Args:
            page_id: ID of the page to modify
            is_trunk: True to make this page a trunk, False to make it a regular page

        Returns:
            The updated page, or None if not found
        """
        # Prevent modifying root pages
        if page_id.startswith("root_"):
            LOGGER.warning("Cannot modify trunk status of root page: %s", page_id)
            return None

        with self._lock:
            # Chronicle エントリの trunk **化** (is_trunk=True) は拒否 — 互換
            # ビュー arasuji_entries は is_trunk=0 の行だけを見せるので、trunk
            # フラグは soft delete と同じ「entry を提示から外す」操作になる
            # (ChronicleProtectedError の docstring)。歯止めの条件は目的
            # (可視性を奪う操作を止める) から引く: is_trunk=False は可視性を
            # 奪わない冪等な安全操作なので通す (既に 0 なら no-op で現在
            # ページが返る)。
            page = get_page(self.conn, page_id)
            if page is not None and page.category == "chronicle" and is_trunk:
                raise ChronicleProtectedError(page_id, "trunk promotion")
            result = set_trunk_flag(self.conn, page_id, is_trunk)
            if result:
                LOGGER.info("Set trunk flag for page %s to %s", page_id, is_trunk)
            return result

    def set_important(self, page_id: str, is_important: bool) -> Optional[MemopediaPage]:
        """
        Set or unset the important flag for a page.

        Important pages will not decay below 'rough' vividness,
        ensuring they remain visible in the persona's context.

        Args:
            page_id: ID of the page to modify
            is_important: True to mark as important

        Returns:
            The updated page, or None if not found
        """
        if page_id.startswith("root_"):
            LOGGER.warning("Cannot modify important status of root page: %s", page_id)
            return None

        with self._lock:
            result = set_important_flag(self.conn, page_id, is_important)
            if result:
                LOGGER.info("Set important flag for page %s to %s", page_id, is_important)
            return result

    def get_trunks(self, category: Optional[str] = None) -> List[MemopediaPage]:
        """
        Get all trunk pages, optionally filtered by category.

        Args:
            category: Optional category filter (CATEGORY_DEFS のキー, e.g. 'people')

        Returns:
            List of trunk pages
        """
        with self._lock:
            return storage_get_trunks(self.conn, category)

    def get_unorganized_pages(self, category: str) -> List[MemopediaPage]:
        """
        Get pages that are direct children of the root (not in any trunk).

        These are pages that haven't been organized into trunks yet.

        Args:
            category: Category to search (CATEGORY_DEFS のキー, e.g. 'people')

        Returns:
            List of unorganized pages
        """
        with self._lock:
            return storage_get_unorganized_pages(self.conn, category)

    def move_pages_to_trunk(
        self,
        page_ids: List[str],
        trunk_id: str,
    ) -> Dict[str, Any]:
        """
        Move multiple pages to a trunk.

        Args:
            page_ids: List of page IDs to move
            trunk_id: ID of the destination trunk page

        Returns:
            {
                "success": True,
                "moved_count": int,
                "trunk_id": str,
                "trunk_title": str
            }
        """
        with self._lock:
            trunk = get_page(self.conn, trunk_id)
            if trunk is None:
                raise ValueError(f"Trunk not found: {trunk_id}")

            moved_count = move_pages_to_parent(self.conn, page_ids, trunk_id)
            LOGGER.info("Moved %d pages to trunk %s (%s)", moved_count, trunk_id, trunk.title)

            return {
                "success": True,
                "moved_count": moved_count,
                "trunk_id": trunk_id,
                "trunk_title": trunk.title,
            }

    def create_trunk(
        self,
        *,
        parent_id: str,
        title: str,
        summary: str = "",
        content: str = "",
        keywords: Optional[List[str]] = None,
        vividness: str = "rough",
        edit_source: Optional[str] = None,
    ) -> MemopediaPage:
        """
        Create a new trunk page.

        A convenience method that creates a page with is_trunk=True.

        Args:
            parent_id: ID of the parent page (usually a root page like 'root_people')
            title: Trunk title
            summary: Trunk summary/description
            content: Trunk content
            keywords: List of keywords
            vividness: Vividness level, default: rough
            edit_source: Source of this edit

        Returns:
            The created trunk page
        """
        return self.create_page(
            parent_id=parent_id,
            title=title,
            summary=summary,
            content=content,
            keywords=keywords,
            vividness=vividness,
            is_trunk=True,
            edit_source=edit_source,
        )

    # ----- Fragment operations -----

    def create_fragment(
        self,
        *,
        entity_id: str,
        content: str,
        chronicle_entry_id: Optional[str] = None,
        source_date: Optional[str] = None,
    ) -> "MemopediaFragment":
        """Create a new fragment linked to an entity page."""
        with self._atomic() as conn:
            frag = storage_create_fragment(
                conn,
                entity_id=entity_id,
                content=content,
                chronicle_entry_id=chronicle_entry_id,
                source_date=source_date,
                commit=False,
            )
            self._touch_page_in_tx(conn, entity_id)
        return frag

    def apply_entity_notes(
        self,
        items: Sequence[EntityNotes],
        *,
        chronicle_entry_id: Optional[str] = None,
        source_date: Optional[str] = None,
        precondition: Optional[Callable[[], None]] = None,
    ) -> List[EntityApplyResult]:
        """抽出 1 回ぶんをまとめて適用する（ページの upsert ＋ Fragment の作成）。

        「同名ページを探す → 無ければ作る／あれば summary を更新する →
        note を Fragment にする」を **1 ロック・1 トランザクション**で行う。
        分けて実行すると二つ壊れる:

        - 探してから作るまでの間に別の書き手が同名ページを作れる（同名の二重作成）
        - 途中で落ちると先行の Fragment だけが残り、拾い直しが同じ知識を
          新しい UUID でもう一度挿す（重複）

        同じ ``chronicle_entry_id`` から同じ文の Fragment が既にあれば作らない
        ——拾い直しの二度目を冪等にする最後の歯止め。

        Args:
            items: 適用するエンティティ（``notes`` も ``summary`` も無い項目は飛ばす）。
            chronicle_entry_id: この抽出を生んだ Chronicle エントリ id。
            source_date: Fragment に刻む日付（``YYYY-MM-DD``）。
            precondition: 書き込みロックを取った**後、何かを書く前**に呼ばれる検査。
                「いま書いてよい状態か」を呼び出し側が確かめる場所で、例外を投げれば
                何も書かれずに戻る。Memopedia は中身を知らない。
                使い所: 抽出の拾い直しは、時間の掛かった実行が「もう自分の担当では
                なくなった」状態で戻ってくることがある。その実行の書き込みをここで
                止める（Codex 三巡 #1）。

        Returns:
            適用結果（入力と同じ並び。飛ばした項目は含まない）。
        """
        results: List[EntityApplyResult] = []
        with self._atomic() as conn:
            if precondition is not None:
                precondition()
            for item in items:
                if not item.notes and not item.summary:
                    continue

                page = find_page_by_title(conn, item.title)
                if page is not None:
                    page_id = page.id
                    is_new = False
                    if item.summary and item.summary != page.summary:
                        # summary の更新は編集履歴に残さない（従来どおり）。
                        # `entity_extractor` 名義の履歴は本文 → Fragment 変換が
                        # 「機械が足した行」の確証に使うので、本文でない文
                        # （summary）を混ぜると誤って自動変換されうる。
                        update_page(
                            conn, page_id, summary=item.summary, commit=False,
                        )
                else:
                    page = self._create_page_in_tx(
                        conn,
                        parent_id=item.parent_id,
                        title=item.title,
                        summary=item.summary,
                        content="",
                        edit_source="entity_extractor",
                    )
                    page_id = page.id
                    is_new = True

                created = 0
                deduped = 0
                for note in item.notes:
                    if fragment_exists(
                        conn,
                        entity_id=page_id,
                        content=note,
                        chronicle_entry_id=chronicle_entry_id,
                        source_date=source_date,
                    ):
                        deduped += 1
                        continue
                    storage_create_fragment(
                        conn,
                        entity_id=page_id,
                        content=note,
                        chronicle_entry_id=chronicle_entry_id,
                        source_date=source_date,
                        commit=False,
                    )
                    created += 1
                self._touch_page_in_tx(conn, page_id)

                results.append(EntityApplyResult(
                    title=item.title,
                    page_id=page_id,
                    is_new_page=is_new,
                    fragments_created=created,
                    fragments_deduped=deduped,
                ))
        return results

    def upsert_page_by_title(
        self,
        *,
        title: str,
        parent_id: str,
        summary: Optional[str] = None,
        append_content: str = "",
        keywords: Optional[List[str]] = None,
        category: Optional[str] = None,
        edit_source: Optional[str] = None,
    ) -> tuple["MemopediaPage", bool]:
        """同名ページがあれば本文を追記し、無ければ作る（1 ロック・1 トランザクション）。

        探すのと書くのを別々のロック区間でやると、その隙間に別の書き手が同じ
        ページを更新／作成できる——後勝ちで片方の追記が消えるか、同名ページが
        二枚できる。

        Args:
            title: ページタイトル（探す鍵）。
            parent_id: 新規作成時の置き場所。
            summary: 与えると要約を差し替える。
            append_content: 既存ページの本文の末尾に足す文章（新規なら本文そのもの）。
            keywords: 新規作成時のキーワード。
            category: タイトル検索をこのカテゴリに絞る（既定は全カテゴリ）。
            edit_source: 編集履歴に記録する入り口の名前。

        Returns:
            ``(page, is_new)``。
        """
        with self._atomic() as conn:
            existing = find_page_by_title(conn, title, category)
            if existing is not None:
                new_content = (
                    existing.content + "\n\n" + append_content
                    if existing.content and append_content
                    else (append_content or existing.content)
                )
                page = self._update_page_in_tx(
                    conn,
                    existing.id,
                    content=new_content,
                    summary=summary if summary is not None else existing.summary,
                    edit_source=edit_source,
                )
                return (page or existing), False

            page = self._create_page_in_tx(
                conn,
                parent_id=parent_id,
                title=title,
                summary=summary or "",
                content=append_content,
                keywords=keywords,
                edit_source=edit_source,
            )
            return page, True

    def get_fragments(
        self,
        entity_id: str,
        *,
        vividness_filter: Optional[List[str]] = None,
    ) -> List["MemopediaFragment"]:
        """Get fragments for an entity page."""
        with self._lock:
            return get_fragments_for_entity(
                self.conn, entity_id,
                vividness_filter=vividness_filter,
            )

    def render_page_body(self, page_id: str) -> str:
        """Render a page's full body text (content + fragments) for LLM consumption.

        Returns content field (manual edits) followed by fragments grouped by date.
        Either or both may be empty.
        """
        with self._lock:
            page = get_page(self.conn, page_id)
            if not page:
                return ""
            fragments = get_fragments_for_entity(self.conn, page.id)
        return self._compose_page_body(page, fragments)

    def page_snapshot(
        self,
        *,
        page_ref: Optional[str] = None,
        title: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """ページ・本文・子ページ一覧を **同じロック区間で一度に**撮る。

        ばらばらに読むと、その合間に他所の書き込みが入り「更新前の本文と更新後の
        子ページ一覧」のような混ざった姿を返しうる。読み手（AI）には一枚の文章に
        見えるので、時点は揃える。

        子ページの取得が失敗したときは例外がそのまま出る——空リストに畳むと
        「子がいない」と読めてしまい、失敗が見えなくなる。

        Args:
            page_ref: ページ id / ``m:N`` / 短縮 id のいずれか。
            title: ``page_ref`` の代わりにタイトルで引く。

        Returns:
            ``{"page": MemopediaPage, "body": str, "children": [MemopediaPage]}``。
            ページが無ければ None。
        """
        with self._lock:
            if page_ref is not None:
                page = get_page(self.conn, page_ref)
            elif title is not None:
                page = find_page_by_title(self.conn, title)
            else:
                raise ValueError("page_snapshot requires page_ref or title")
            if page is None:
                return None
            fragments = get_fragments_for_entity(self.conn, page.id)
            children = get_children(self.conn, page.id)
        return {
            "page": page,
            "body": self._compose_page_body(page, fragments),
            "children": children,
        }

    @staticmethod
    def _compose_page_body(
        page: MemopediaPage, fragments: List["MemopediaFragment"],
    ) -> str:
        """本文（手書き）と Fragment を一つの文章に組む。

        手書き本文は **原文のまま**出す。先頭の字下げ・行末の空白（Markdown の
        改行）・末尾の改行は書いた人の表現であって、表示の都合で削ってよいもの
        ではない（削ると LLM が読む本文が書かれたものと変わる）。空かどうかの
        判定にだけ strip を使う。
        """
        parts: List[str] = []

        if page.content and page.content.strip():
            parts.append(page.content)

        if fragments:
            grouped: dict[str, list[str]] = {}
            for f in fragments:
                key = f.source_date or "unknown"
                if key not in grouped:
                    grouped[key] = []
                grouped[key].append(f.content)

            frag_lines: List[str] = []
            for date, notes in grouped.items():
                frag_lines.append(f"## {date}")
                for note in notes:
                    frag_lines.append(f"- {note}")
            parts.append("\n".join(frag_lines))

        return "\n\n".join(parts)
