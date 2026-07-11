"""P4-a2 編纂実行部・翌朝報告・新聞窓集計のテスト。

検証対象:
- execute_merge: 逐語結合（両者の本文が結合後に含まれる）、キーワード和集合、
  子の付け替え、吸収側 soft-delete、編集来歴に curation 刻印
- execute_split: 保存則（子＋残り＝元、ブロック単位完全一致）、
  LLM が不正な割当を返した時に棄却して ValueError になる（mock LLM 必須）
- run_pending_plans: done/failed 遷移・一部失敗が他を止めない
- event_message: タグ規約どおり（["internal", "event_message", "curation"]）に書かれる
- 新聞「棚の整理」節: 窓内の編纂編集が載る・ai_conversation が載らない・編集ゼロで「なし」
- 窓時刻: save_day_report で保存、次回 generate_day_report で読まれる
"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, patch

import pytest

from sai_memory.curation_ops import (
    STATUS_DONE,
    STATUS_FAILED,
    STATUS_PENDING,
    _split_into_blocks,
    enqueue_plan,
    execute_merge,
    execute_split,
    init_curation_tables,
    list_pending,
    run_pending_plans,
)


# ---------------------------------------------------------------------------
# in-memory DB ヘルパ
# ---------------------------------------------------------------------------


def _make_full_conn() -> sqlite3.Connection:
    """memopedia_pages / memopedia_page_edit_history / curation_plans を含む in-memory DB。"""
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.execute(
        """
        CREATE TABLE memopedia_pages (
            id           TEXT PRIMARY KEY,
            parent_id    TEXT,
            title        TEXT NOT NULL,
            summary      TEXT DEFAULT '',
            content      TEXT DEFAULT '',
            category     TEXT NOT NULL,
            created_at   INTEGER NOT NULL,
            updated_at   INTEGER NOT NULL,
            keywords     TEXT DEFAULT '[]',
            metadata     TEXT,
            is_deleted   INTEGER DEFAULT 0,
            vividness    TEXT DEFAULT 'rough',
            is_trunk     INTEGER DEFAULT 0,
            is_important INTEGER DEFAULT 0,
            last_referenced_at INTEGER,
            short_id     INTEGER
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE memopedia_page_edit_history (
            id                   TEXT PRIMARY KEY,
            page_id              TEXT NOT NULL,
            edited_at            INTEGER NOT NULL,
            diff_text            TEXT NOT NULL,
            ref_start_message_id TEXT,
            ref_end_message_id   TEXT,
            edit_type            TEXT NOT NULL,
            edit_source          TEXT,
            before_title         TEXT,
            before_summary       TEXT,
            before_content       TEXT
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_edit_history_page "
        "ON memopedia_page_edit_history(page_id)"
    )
    # desk_items テーブルも追加（move_pages_to_parent が外部制約なしでも動く）
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS memopedia_page_states (
            thread_id TEXT,
            page_id   TEXT,
            is_open   INTEGER DEFAULT 0,
            opened_at INTEGER,
            PRIMARY KEY (thread_id, page_id)
        )
        """
    )
    init_curation_tables(conn)
    conn.commit()
    return conn


_SID = [1]


def _insert_page(
    conn: sqlite3.Connection,
    *,
    page_id: str,
    title: str,
    category: str = "people",
    content: str = "",
    summary: str = "",
    parent_id: Optional[str] = None,
    keywords: Optional[List[str]] = None,
    is_trunk: bool = False,
    is_deleted: bool = False,
    short_id: Optional[int] = None,
) -> None:
    now = int(time.time())
    if short_id is None:
        short_id = _SID[0]
        _SID[0] += 1
    conn.execute(
        """
        INSERT INTO memopedia_pages
            (id, parent_id, title, category, content, summary, keywords,
             is_trunk, is_deleted, created_at, updated_at,
             last_referenced_at, short_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            page_id, parent_id, title, category, content, summary,
            json.dumps(keywords or []),
            1 if is_trunk else 0,
            1 if is_deleted else 0,
            now - 3600, now - 3600, now - 3600,
            short_id,
        ),
    )
    conn.commit()


def _record_edit(
    conn: sqlite3.Connection,
    *,
    page_id: str,
    edit_source: str,
    edited_at: Optional[int] = None,
) -> None:
    import uuid as _uuid
    now = edited_at if edited_at is not None else int(time.time())
    conn.execute(
        """
        INSERT INTO memopedia_page_edit_history
            (id, page_id, edited_at, diff_text, edit_type, edit_source)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (_uuid.uuid4().hex, page_id, now, "diff", "update", edit_source),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Memopedia スタブ（full DB 版）
# ---------------------------------------------------------------------------


def _make_memopedia_stub(conn: sqlite3.Connection) -> Any:
    """最小限の Memopedia スタブ（テスト用）。実 storage 関数に委譲する。"""
    from sai_memory.memopedia import storage as _st

    class _Stub:
        def __init__(self, conn):
            self._conn = conn

        def update_page(self, page_id, *, content=None, keywords=None, edit_source=None, **kw):
            old = _st.get_page(self._conn, page_id)
            if old is None:
                return None
            _st.update_page(self._conn, page_id, content=content, keywords=keywords)
            # 編集来歴を記録
            import uuid as _uuid
            self._conn.execute(
                """
                INSERT INTO memopedia_page_edit_history
                    (id, page_id, edited_at, diff_text, edit_type, edit_source,
                     before_title, before_summary, before_content)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    _uuid.uuid4().hex, page_id, int(time.time()),
                    "curation_diff", "update", edit_source,
                    old.title, old.summary, old.content,
                ),
            )
            self._conn.commit()
            return _st.get_page(self._conn, page_id)

        def create_page(self, *, parent_id, title, content="", edit_source=None, **kw):
            parent = _st.get_page(self._conn, parent_id)
            if parent is None:
                raise ValueError(f"parent not found: {parent_id}")
            page = _st.create_page(
                self._conn,
                parent_id=parent_id,
                title=title,
                content=content,
                category=parent.category,
            )
            import uuid as _uuid
            self._conn.execute(
                """
                INSERT INTO memopedia_page_edit_history
                    (id, page_id, edited_at, diff_text, edit_type, edit_source,
                     before_title, before_summary, before_content)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    _uuid.uuid4().hex, page.id, int(time.time()),
                    "create_diff", "create", edit_source, "", "", "",
                ),
            )
            self._conn.commit()
            return page

        def delete_page(self, page_id, edit_source=None, **kw):
            import uuid as _uuid
            page = _st.get_page(self._conn, page_id)
            if page is None:
                return False
            self._conn.execute(
                """
                INSERT INTO memopedia_page_edit_history
                    (id, page_id, edited_at, diff_text, edit_type, edit_source,
                     before_title, before_summary, before_content)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    _uuid.uuid4().hex, page_id, int(time.time()),
                    "delete_diff", "delete", edit_source,
                    page.title, page.summary, page.content,
                ),
            )
            now = int(time.time())
            self._conn.execute(
                "UPDATE memopedia_pages SET is_deleted = 1, updated_at = ? WHERE id = ?",
                (now, page_id),
            )
            self._conn.commit()
            return True

    return _Stub(conn)


# ---------------------------------------------------------------------------
# _split_into_blocks のテスト
# ---------------------------------------------------------------------------


class TestSplitIntoBlocks:
    def test_empty_content_returns_empty(self):
        assert _split_into_blocks("") == []

    def test_single_paragraph(self):
        blocks = _split_into_blocks("Hello world")
        assert blocks == ["Hello world"]

    def test_two_paragraphs(self):
        blocks = _split_into_blocks("Para1\n\nPara2")
        assert len(blocks) == 2
        assert "Para1" in blocks[0]
        assert "Para2" in blocks[1]

    def test_heading_attached_to_next_block(self):
        content = "前書き\n\n## 章タイトル\n\n本文"
        blocks = _split_into_blocks(content)
        # "## 章タイトル" は "本文" の先頭に付くはず
        assert any("## 章タイトル" in b and "本文" in b for b in blocks)

    def test_roundtrip_reconstruction(self):
        """ブロックを連結すれば元の本文の全テキストが復元できる（保存則の基礎）。"""
        content = "段落A\n\n段落B\n\n段落C"
        blocks = _split_into_blocks(content)
        all_text = "\n\n".join(blocks)
        # 各段落のテキストが含まれること（空白の扱いは許容）
        for para in ["段落A", "段落B", "段落C"]:
            assert para in all_text


# ---------------------------------------------------------------------------
# execute_merge のテスト
# ---------------------------------------------------------------------------


class TestExecuteMerge:
    def _setup(self):
        conn = _make_full_conn()
        _insert_page(conn, page_id="root", title="root", is_trunk=True, short_id=0)
        _insert_page(
            conn, page_id="survivor", title="SAIVerse",
            content="SAIVerse本文", summary="サマリA",
            keywords=["A", "B"], parent_id="root", short_id=1,
        )
        _insert_page(
            conn, page_id="absorbed", title="SAIVerseの詳細",
            content="詳細本文", summary="サマリB",
            keywords=["B", "C"], parent_id="root", short_id=2,
        )
        memopedia = _make_memopedia_stub(conn)
        return conn, memopedia

    def test_merged_content_contains_both_bodies(self):
        conn, memopedia = self._setup()
        execute_merge(conn, "survivor", "absorbed", memopedia)
        from sai_memory.memopedia.storage import get_page
        survivor = get_page(conn, "survivor")
        assert "SAIVerse本文" in survivor.content
        assert "詳細本文" in survivor.content

    def test_merged_content_contains_absorbed_summary(self):
        conn, memopedia = self._setup()
        execute_merge(conn, "survivor", "absorbed", memopedia)
        from sai_memory.memopedia.storage import get_page
        survivor = get_page(conn, "survivor")
        # absorbed の summary も含まれるはず
        assert "サマリB" in survivor.content

    def test_keywords_are_union(self):
        conn, memopedia = self._setup()
        execute_merge(conn, "survivor", "absorbed", memopedia)
        from sai_memory.memopedia.storage import get_page
        survivor = get_page(conn, "survivor")
        assert set(survivor.keywords) == {"A", "B", "C"}

    def test_absorbed_is_soft_deleted(self):
        conn, memopedia = self._setup()
        execute_merge(conn, "survivor", "absorbed", memopedia)
        cur = conn.execute(
            "SELECT is_deleted FROM memopedia_pages WHERE id = 'absorbed'"
        )
        row = cur.fetchone()
        assert row is not None
        assert row[0] == 1, "吸収側は soft-delete されるべき"

    def test_edit_source_is_curation_in_history(self):
        conn, memopedia = self._setup()
        execute_merge(conn, "survivor", "absorbed", memopedia)
        cur = conn.execute(
            "SELECT edit_source FROM memopedia_page_edit_history "
            "WHERE page_id = 'survivor'"
        )
        sources = [r[0] for r in cur.fetchall()]
        assert "curation" in sources, f"edit_source に 'curation' が含まれない: {sources}"

    def test_children_are_reparented(self):
        conn = _make_full_conn()
        _insert_page(conn, page_id="root", title="root", is_trunk=True, short_id=0)
        _insert_page(conn, page_id="survivor", title="残る", content="A", parent_id="root", short_id=1)
        _insert_page(conn, page_id="absorbed", title="消える", content="B", parent_id="root", short_id=2)
        # absorbed の子ページ
        _insert_page(conn, page_id="child1", title="子1", content="C", parent_id="absorbed", short_id=3)
        _insert_page(conn, page_id="child2", title="子2", content="D", parent_id="absorbed", short_id=4)

        memopedia = _make_memopedia_stub(conn)
        result = execute_merge(conn, "survivor", "absorbed", memopedia)

        assert result["children_moved"] == 2

        # child1, child2 の親が survivor になっているはず
        for child_id in ("child1", "child2"):
            cur = conn.execute(
                "SELECT parent_id FROM memopedia_pages WHERE id = ?", (child_id,)
            )
            row = cur.fetchone()
            assert row is not None
            assert row[0] == "survivor", f"{child_id} の親が survivor でない: {row[0]}"

    def test_same_id_raises(self):
        conn = _make_full_conn()
        _insert_page(conn, page_id="p", title="P", content="X", short_id=1)
        memopedia = _make_memopedia_stub(conn)
        with pytest.raises(ValueError, match="同じ ID"):
            execute_merge(conn, "p", "p", memopedia)

    def test_missing_page_raises(self):
        conn = _make_full_conn()
        _insert_page(conn, page_id="existing", title="existing", content="X", short_id=1)
        memopedia = _make_memopedia_stub(conn)
        with pytest.raises(ValueError):
            execute_merge(conn, "existing", "no_such_page", memopedia)


# ---------------------------------------------------------------------------
# execute_split のテスト
# ---------------------------------------------------------------------------


def _make_mock_llm(response: Dict[str, Any]) -> MagicMock:
    """LLM の generate を mock する。response は structured output の dict。"""
    client = MagicMock()
    client.generate.return_value = response
    return client


class TestExecuteSplit:
    def _setup(self, content: str) -> tuple:
        conn = _make_full_conn()
        _insert_page(
            conn, page_id="root", title="root", is_trunk=True, short_id=0
        )
        _insert_page(
            conn, page_id="target", title="大きなページ",
            content=content, parent_id="root", short_id=1,
        )
        memopedia = _make_memopedia_stub(conn)
        return conn, memopedia

    def test_conservation_law_holds(self):
        """子ページ全部＋親の残り＝元の全ブロック（各ブロックがちょうど一度）。"""
        content = "ブロック0\n\nブロック1\n\nブロック2\n\nブロック3"
        conn, memopedia = self._setup(content)

        # LLM: ブロック0,1 → 子A、ブロック2 → 子B、残り: ブロック3
        llm = _make_mock_llm({
            "sections": [
                {"title": "子A", "block_indices": [0, 1]},
                {"title": "子B", "block_indices": [2]},
            ],
            "remaining_block_indices": [3],
        })

        result = execute_split(conn, "target", memopedia, llm)

        assert result["total_blocks"] == 4
        assert len(result["sections"]) == 2
        assert result["remaining_block_count"] == 1

        # 子ページの本文にブロックが含まれていることを確認
        from sai_memory.memopedia.storage import get_children
        children = get_children(conn, "target")
        assert len(children) == 2

        child_titles = {c.title for c in children}
        assert "子A" in child_titles
        assert "子B" in child_titles

        child_a = next(c for c in children if c.title == "子A")
        assert "ブロック0" in child_a.content
        assert "ブロック1" in child_a.content

        child_b = next(c for c in children if c.title == "子B")
        assert "ブロック2" in child_b.content

        # 親の残りにブロック3が含まれること
        from sai_memory.memopedia.storage import get_page
        parent = get_page(conn, "target")
        assert "ブロック3" in parent.content

    def test_conservation_law_violation_raises(self):
        """LLM が不正な割当（ブロックの漏れ/重複/範囲外）を返した場合に棄却して ValueError。"""
        content = "A\n\nB\n\nC"
        conn, memopedia = self._setup(content)

        # ブロック 0,1,2 があるのに 0,1 しか割り当てず remaining も空 → ブロック2 が漏れる
        llm = _make_mock_llm({
            "sections": [
                {"title": "子A", "block_indices": [0, 1]},
            ],
            "remaining_block_indices": [],  # ブロック2 が消える → 保存則違反
        })

        with pytest.raises(ValueError, match="保存則違反"):
            execute_split(conn, "target", memopedia, llm)

    def test_conservation_law_duplicate_raises(self):
        """LLM が同じブロックを複数回割り当てた場合に棄却。"""
        content = "A\n\nB"
        conn, memopedia = self._setup(content)

        # ブロック0 を2回使用
        llm = _make_mock_llm({
            "sections": [
                {"title": "子A", "block_indices": [0]},
                {"title": "子B", "block_indices": [0]},  # 重複
            ],
            "remaining_block_indices": [1],
        })

        with pytest.raises(ValueError, match="保存則違反"):
            execute_split(conn, "target", memopedia, llm)

    def test_single_block_raises(self):
        """段落が1つしかない場合は分割不可。"""
        conn, memopedia = self._setup("段落が1つだけ")
        llm = _make_mock_llm({
            "sections": [{"title": "子", "block_indices": [0]}],
            "remaining_block_indices": [],
        })
        with pytest.raises(ValueError, match="1 つしかない"):
            execute_split(conn, "target", memopedia, llm)

    def test_llm_failure_raises(self):
        """LLM が例外を投げた場合は ValueError に変換される。"""
        content = "A\n\nB\n\nC"
        conn, memopedia = self._setup(content)
        llm = MagicMock()
        llm.generate.side_effect = RuntimeError("LLM down")
        with pytest.raises(ValueError, match="LLM 呼び出しに失敗"):
            execute_split(conn, "target", memopedia, llm)

    def test_edit_source_is_curation_for_child(self):
        """分割で作成した子ページの編集来歴に edit_source='curation' が刻まれる。"""
        content = "ブロック0\n\nブロック1"
        conn, memopedia = self._setup(content)
        llm = _make_mock_llm({
            "sections": [{"title": "子A", "block_indices": [0]}],
            "remaining_block_indices": [1],
        })
        execute_split(conn, "target", memopedia, llm)

        from sai_memory.memopedia.storage import get_children
        children = get_children(conn, "target")
        assert len(children) == 1
        child_id = children[0].id
        cur = conn.execute(
            "SELECT edit_source FROM memopedia_page_edit_history WHERE page_id = ?",
            (child_id,),
        )
        sources = [r[0] for r in cur.fetchall()]
        assert "curation" in sources, f"子ページの edit_source に 'curation' が無い: {sources}"


# ---------------------------------------------------------------------------
# run_pending_plans のテスト
# ---------------------------------------------------------------------------


def _make_run_manager(conn: sqlite3.Connection) -> Any:
    """run_pending_plans が必要とする最小スタブ。"""
    fake_messages: List[Dict[str, Any]] = []

    class _FakeAdapter:
        def __init__(self, c):
            self.conn = c
            self.memopedia = _make_memopedia_stub(c)
            self._messages = fake_messages

        def append_persona_message(self, msg: Dict[str, Any]) -> None:
            self._messages.append(msg)

    adapter = _FakeAdapter(conn)
    persona_obj = SimpleNamespace(
        sai_memory=adapter,
        LIGHTWEIGHT_MODEL=None,
    )
    return SimpleNamespace(
        personas={"alice": persona_obj},
        SessionLocal=None,
    ), fake_messages


class TestRunPendingPlans:
    def test_merge_plan_executes_and_becomes_done(self):
        conn = _make_full_conn()
        _insert_page(conn, page_id="root", title="root", is_trunk=True, short_id=0)
        _insert_page(
            conn, page_id="p_s", title="残る", content="本文A", parent_id="root", short_id=1,
        )
        _insert_page(
            conn, page_id="p_a", title="消える", content="本文B", parent_id="root", short_id=2,
        )
        # enqueue
        enqueue_plan(conn, kind="merge", op_id="merge:m:1+m:2", refs=["m:1", "m:2"])
        assert len(list_pending(conn)) == 1

        manager, messages = _make_run_manager(conn)
        result = run_pending_plans(manager, "alice")

        assert len(result["done"]) == 1
        assert len(result["failed"]) == 0

        # plan は done になっている
        cur = conn.execute(
            "SELECT status FROM curation_plans WHERE op_id = ?",
            ("merge:m:1+m:2",),
        )
        row = cur.fetchone()
        assert row is not None
        assert row[0] == STATUS_DONE

        # pending が空になっている
        assert list_pending(conn) == []

    def test_failed_plan_does_not_stop_others(self):
        """最初のプランが失敗しても 2 番目は実行される。"""
        conn = _make_full_conn()
        _insert_page(conn, page_id="root", title="root", is_trunk=True, short_id=0)
        _insert_page(
            conn, page_id="p_s", title="残る", content="本文A", parent_id="root", short_id=1,
        )
        _insert_page(
            conn, page_id="p_a", title="消える", content="本文B", parent_id="root", short_id=2,
        )

        # 1番目: 存在しないページを参照 → 失敗
        enqueue_plan(conn, kind="merge", op_id="merge:no_such+m:2", refs=["m:999", "m:2"])
        # 2番目: 正常
        enqueue_plan(conn, kind="merge", op_id="merge:m:1+m:2", refs=["m:1", "m:2"])

        manager, messages = _make_run_manager(conn)
        result = run_pending_plans(manager, "alice")

        assert len(result["failed"]) == 1
        assert len(result["done"]) == 1

    def test_event_message_tags_are_correct(self):
        """event_message のタグが規約どおりであること。"""
        conn = _make_full_conn()
        _insert_page(conn, page_id="root", title="root", is_trunk=True, short_id=0)
        _insert_page(
            conn, page_id="p_s", title="残る", content="本文A", parent_id="root", short_id=1,
        )
        _insert_page(
            conn, page_id="p_a", title="消える", content="本文B", parent_id="root", short_id=2,
        )
        enqueue_plan(conn, kind="merge", op_id="merge:m:1+m:2", refs=["m:1", "m:2"])

        manager, messages = _make_run_manager(conn)
        run_pending_plans(manager, "alice")

        assert len(messages) == 1, f"event_message が書かれていない: {messages}"
        msg = messages[0]
        # role は "user"（機構の名義）
        assert msg.get("role") == "user", f"role が 'user' でない: {msg.get('role')}"
        # タグ規約
        tags = (msg.get("metadata") or {}).get("tags") or []
        assert "event_message" in tags, f"'event_message' タグがない: {tags}"
        assert "curation" in tags, f"'curation' タグがない: {tags}"
        assert "internal" in tags, f"'internal' タグがない: {tags}"
        # 内容に報告が含まれる
        content = msg.get("content") or ""
        assert "棚の整理" in content or "統合" in content, f"報告が含まれない: {content[:100]}"

    def test_no_pending_plans_no_event_message(self):
        """pending が0件なら event_message は書かれない。"""
        conn = _make_full_conn()
        manager, messages = _make_run_manager(conn)
        result = run_pending_plans(manager, "alice")
        assert result["done"] == []
        assert result["failed"] == []
        assert messages == []


# ---------------------------------------------------------------------------
# 新聞の「棚の整理」節のテスト
# ---------------------------------------------------------------------------


def _make_persona_with_conn(conn: sqlite3.Connection) -> Any:
    """_section_curation_edits が触る最小 persona スタブ。"""
    adapter = SimpleNamespace(conn=conn)
    return SimpleNamespace(sai_memory=adapter)


class TestSectionCurationEdits:
    def test_curation_edit_is_listed(self):
        """窓内の curation 編集が節に載る。"""
        from saiverse.day_report import _section_curation_edits

        conn = _make_full_conn()
        _insert_page(conn, page_id="p1", title="テストページ", short_id=1)
        since = int(time.time()) - 60  # 1分前
        _record_edit(conn, page_id="p1", edit_source="curation")

        persona = _make_persona_with_conn(conn)
        lines = _section_curation_edits(persona, since)
        text = "\n".join(lines)

        assert "棚の整理（編纂）" in text or "curation" in text.lower()
        assert "なし" not in text

    def test_ai_conversation_is_excluded(self):
        """ai_conversation は載らない（セッションダイジェスト欄と重複するため）。"""
        from saiverse.day_report import _section_curation_edits

        conn = _make_full_conn()
        _insert_page(conn, page_id="p1", title="会話ページ", short_id=1)
        since = int(time.time()) - 60
        # ai_conversation のみ
        _record_edit(conn, page_id="p1", edit_source="ai_conversation")

        persona = _make_persona_with_conn(conn)
        lines = _section_curation_edits(persona, since)
        text = "\n".join(lines)
        # "なし" または空
        assert "ai_conversation" not in text
        # 実質コンテンツなし → "なし"
        assert NONE_TEXT_FRAGMENT in text or "なし" in text

    def test_zero_edits_shows_none(self):
        """編集ゼロで「なし」が出る。"""
        from saiverse.day_report import _section_curation_edits

        conn = _make_full_conn()
        since = int(time.time()) - 60

        persona = _make_persona_with_conn(conn)
        lines = _section_curation_edits(persona, since)
        text = "\n".join(lines)
        assert "なし" in text

    def test_edits_before_window_are_excluded(self):
        """窓より前の編集は載らない。"""
        from saiverse.day_report import _section_curation_edits

        conn = _make_full_conn()
        _insert_page(conn, page_id="p1", title="古いページ", short_id=1)

        old_time = int(time.time()) - 3600  # 1時間前
        since = int(time.time()) - 60  # 1分前が窓の起点
        _record_edit(conn, page_id="p1", edit_source="curation", edited_at=old_time)

        persona = _make_persona_with_conn(conn)
        lines = _section_curation_edits(persona, since)
        text = "\n".join(lines)
        assert "なし" in text


NONE_TEXT_FRAGMENT = "なし"


# ---------------------------------------------------------------------------
# 窓時刻の永続化テスト
# ---------------------------------------------------------------------------


class TestWindowTimestamp:
    def test_save_and_read_roundtrip(self, tmp_path: Path):
        """save_last_generated_at → get_last_generated_at で値が戻る。"""
        from saiverse.day_report import _get_last_generated_at, _save_last_generated_at

        ts = int(time.time())
        _save_last_generated_at("alice", ts, base_dir=tmp_path)
        recovered = _get_last_generated_at("alice", base_dir=tmp_path)
        assert recovered == ts

    def test_missing_file_returns_none(self, tmp_path: Path):
        """ファイルがなければ None。"""
        from saiverse.day_report import _get_last_generated_at

        result = _get_last_generated_at("nobody", base_dir=tmp_path)
        assert result is None


# ---------------------------------------------------------------------------
# execute_merge — metadata 引き継ぎのテスト
# ---------------------------------------------------------------------------


class TestExecuteMergeMetadata:
    def test_absorbed_persona_id_is_inherited_by_survivor(self):
        """吸収側だけが persona_id を持つ場合、統合後の survivor に persona_id が引き継がれる。

        背景: extractor 製のページ（metadata なし）が survivor になり、
        再会システム製のページ（persona_id を持つ）が absorbed になるケースが実データに存在する。
        この場合に和集合にしないと get_page_by_persona_id が個人ページを見失う。
        """
        conn = _make_full_conn()
        _insert_page(conn, page_id="root", title="root", is_trunk=True, short_id=0)
        # survivor: extractor 製（metadata なし）
        conn.execute(
            """
            INSERT INTO memopedia_pages
                (id, parent_id, title, category, content, summary, keywords,
                 is_trunk, is_deleted, created_at, updated_at,
                 last_referenced_at, short_id, metadata)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "survivor", "root", "田中エア", "people", "extractor製の本文", "", "[]",
                0, 0,
                int(time.time()) - 7200, int(time.time()) - 7200, int(time.time()) - 7200,
                1, None,  # metadata は NULL
            ),
        )
        conn.commit()
        # absorbed: 再会システム製（persona_id を持つ）
        conn.execute(
            """
            INSERT INTO memopedia_pages
                (id, parent_id, title, category, content, summary, keywords,
                 is_trunk, is_deleted, created_at, updated_at,
                 last_referenced_at, short_id, metadata)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "absorbed", "root", "田中エア（再会）", "people", "再会システム製の本文", "", "[]",
                0, 0,
                int(time.time()) - 3600, int(time.time()) - 3600, int(time.time()) - 3600,
                2, json.dumps({"persona_id": "air_city_a"}),  # persona_id を持つ
            ),
        )
        conn.commit()

        memopedia = _make_memopedia_stub(conn)
        execute_merge(conn, "survivor", "absorbed", memopedia)

        from sai_memory.memopedia.storage import get_page
        survivor_after = get_page(conn, "survivor")
        assert survivor_after is not None
        assert survivor_after.metadata is not None, "metadata が None: persona_id が失われた"
        assert survivor_after.metadata.get("persona_id") == "air_city_a", (
            f"persona_id が引き継がれていない: {survivor_after.metadata}"
        )

    def test_survivor_persona_id_is_not_overwritten(self):
        """survivor と absorbed の両方が persona_id を持つ場合、survivor の値が優先される。"""
        conn = _make_full_conn()
        _insert_page(conn, page_id="root", title="root", is_trunk=True, short_id=0)
        conn.execute(
            """
            INSERT INTO memopedia_pages
                (id, parent_id, title, category, content, summary, keywords,
                 is_trunk, is_deleted, created_at, updated_at,
                 last_referenced_at, short_id, metadata)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "survivor", "root", "残る人", "people", "本文A", "", "[]",
                0, 0,
                int(time.time()) - 7200, int(time.time()) - 7200, int(time.time()) - 7200,
                1, json.dumps({"persona_id": "survivor_persona"}),
            ),
        )
        conn.execute(
            """
            INSERT INTO memopedia_pages
                (id, parent_id, title, category, content, summary, keywords,
                 is_trunk, is_deleted, created_at, updated_at,
                 last_referenced_at, short_id, metadata)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "absorbed", "root", "消える人", "people", "本文B", "", "[]",
                0, 0,
                int(time.time()) - 3600, int(time.time()) - 3600, int(time.time()) - 3600,
                2, json.dumps({"persona_id": "absorbed_persona"}),
            ),
        )
        conn.commit()

        memopedia = _make_memopedia_stub(conn)
        execute_merge(conn, "survivor", "absorbed", memopedia)

        from sai_memory.memopedia.storage import get_page
        survivor_after = get_page(conn, "survivor")
        assert survivor_after is not None
        assert survivor_after.metadata is not None
        # survivor の persona_id が保持されていること
        assert survivor_after.metadata.get("persona_id") == "survivor_persona", (
            f"survivor の persona_id が上書きされた: {survivor_after.metadata}"
        )

    def test_absorbed_extra_keys_are_merged(self):
        """absorbed のキーで survivor が持っていないものは和集合として残る。"""
        conn = _make_full_conn()
        _insert_page(conn, page_id="root", title="root", is_trunk=True, short_id=0)
        conn.execute(
            """
            INSERT INTO memopedia_pages
                (id, parent_id, title, category, content, summary, keywords,
                 is_trunk, is_deleted, created_at, updated_at,
                 last_referenced_at, short_id, metadata)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "survivor", "root", "残る", "people", "本文A", "", "[]",
                0, 0,
                int(time.time()) - 7200, int(time.time()) - 7200, int(time.time()) - 7200,
                1, json.dumps({"foo": "survivor_foo"}),
            ),
        )
        conn.execute(
            """
            INSERT INTO memopedia_pages
                (id, parent_id, title, category, content, summary, keywords,
                 is_trunk, is_deleted, created_at, updated_at,
                 last_referenced_at, short_id, metadata)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "absorbed", "root", "消える", "people", "本文B", "", "[]",
                0, 0,
                int(time.time()) - 3600, int(time.time()) - 3600, int(time.time()) - 3600,
                2, json.dumps({"bar": "absorbed_bar"}),
            ),
        )
        conn.commit()

        memopedia = _make_memopedia_stub(conn)
        execute_merge(conn, "survivor", "absorbed", memopedia)

        from sai_memory.memopedia.storage import get_page
        survivor_after = get_page(conn, "survivor")
        assert survivor_after is not None
        meta = survivor_after.metadata or {}
        # survivor のキーも absorbed のキーも残る
        assert meta.get("foo") == "survivor_foo"
        assert meta.get("bar") == "absorbed_bar"
