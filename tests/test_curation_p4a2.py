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
    # 実 storage 層の残りテーブル作成＋ルートページ seed（冪等）。
    # run_pending_plans は実 Memopedia を初期化する（seed が走る）ため、
    # テスト前後のスナップショット比較が seed 差分で汚れないよう
    # ここで先に済ませておく。
    from sai_memory.memopedia.storage import init_memopedia_tables
    init_memopedia_tables(conn)
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
    created_at: Optional[int] = None,
    updated_at: Optional[int] = None,
    last_referenced_at: Optional[int] = None,
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
            created_at if created_at is not None else now - 3600,
            updated_at if updated_at is not None else now - 3600,
            last_referenced_at if last_referenced_at is not None else now - 3600,
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

        def create_page(self, *, parent_id, title, summary="", content="",
                        edit_source=None, **kw):
            parent = _st.get_page(self._conn, parent_id)
            if parent is None:
                raise ValueError(f"parent not found: {parent_id}")
            page = _st.create_page(
                self._conn,
                parent_id=parent_id,
                title=title,
                summary=summary,
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
        """全ブロックの素の連結が元本文に文字レベルで完全一致する（本文保存則の基礎）。"""
        content = "段落A\n\n段落B\n\n段落C"
        blocks = _split_into_blocks(content)
        assert "".join(blocks) == content

    def test_lossless_on_whitespace_edge_cases(self):
        """先頭末尾空白・空白のみの行・3個以上の連続改行も失わない（修正3の回帰）。"""
        cases = [
            "  A  \n\n\nB  \n",               # 先頭末尾空白・3連改行・末尾改行
            "A\n\n\n\nB",                      # 4連改行
            "\n\nA\n\nB\n\n",                  # 先頭・末尾が区切り
            "前書き\n\n## 章タイトル\n\n本文",  # 見出し繰り越し
            "   \n\n  \n\nA",                  # 空白のみのチャンク
            "単一段落",
        ]
        for content in cases:
            blocks = _split_into_blocks(content)
            assert "".join(blocks) == content, (
                f"lossless 違反: {content!r} → {blocks!r}"
            )


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

    def test_unclaimed_block_stays_in_parent(self):
        """どの子も挙げなかったブロックは棄却でなく親に残る。"""
        content = "A\n\nB\n\nC"
        conn, memopedia = self._setup(content)

        # ブロック 0,1 しか挙げない（旧実装では「ブロック2 が漏れた」として棄却していた）
        llm = _make_mock_llm({
            "sections": [
                {"title": "子A", "block_indices": [0, 1]},
            ],
        })

        result = execute_split(conn, "target", memopedia, llm)

        assert result["remaining_block_count"] == 1
        from sai_memory.memopedia.storage import get_page, get_children
        assert "C" in get_page(conn, "target").content
        children = get_children(conn, "target")
        assert len(children) == 1
        assert "A" in children[0].content and "B" in children[0].content

    def test_duplicate_claim_stays_in_parent(self):
        """重複はプロンプトで禁じてあるが、破られた場合の安全網として親に残す。"""
        content = "A\n\nB\n\nC"
        conn, memopedia = self._setup(content)

        # ブロック0 を子A・子B の両方が挙げる（旧実装では棄却していた）
        llm = _make_mock_llm({
            "sections": [
                {"title": "子A", "block_indices": [0, 1]},
                {"title": "子B", "block_indices": [0, 2]},  # 0 が競合
            ],
        })

        result = execute_split(conn, "target", memopedia, llm)

        # 競合した 0 は親へ。1 は子A、2 は子B。
        assert result["remaining_block_count"] == 1
        from sai_memory.memopedia.storage import get_page, get_children
        parent = get_page(conn, "target")
        assert "A" in parent.content
        children = {c.title: c.content for c in get_children(conn, "target")}
        assert "B" in children["子A"] and "A" not in children["子A"]
        assert "C" in children["子B"] and "A" not in children["子B"]

    def test_out_of_range_index_is_ignored(self):
        """範囲外・非整数の番号は無視され、該当ブロックは親に残る。"""
        content = "A\n\nB"
        conn, memopedia = self._setup(content)

        llm = _make_mock_llm({
            "sections": [
                {"title": "子A", "block_indices": [0, 99, -3]},
            ],
        })

        result = execute_split(conn, "target", memopedia, llm)

        assert result["total_blocks"] == 2
        assert result["remaining_block_count"] == 1  # B のみ
        from sai_memory.memopedia.storage import get_children
        children = get_children(conn, "target")
        assert len(children) == 1
        assert children[0].content.strip() == "A"

    def test_child_with_only_conflicting_blocks_is_not_created(self):
        """挙げたブロックが全て競合した子ページは作られない（空ページを作らない）。"""
        content = "A\n\nB"
        conn, memopedia = self._setup(content)

        llm = _make_mock_llm({
            "sections": [
                {"title": "子A", "block_indices": [0, 1]},
                {"title": "子B", "block_indices": [0, 1]},  # 全ブロックが競合
            ],
        })

        result = execute_split(conn, "target", memopedia, llm)

        assert result["sections"] == []
        assert result["remaining_block_count"] == 2
        from sai_memory.memopedia.storage import get_children, get_page
        assert get_children(conn, "target") == []
        assert get_page(conn, "target").content == content

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
        memory_weave_model=None,
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
        enqueue_plan(conn, kind="merge", op_id="merge:memopedia:1+memopedia:2", refs=["memopedia:1", "memopedia:2"])
        assert len(list_pending(conn)) == 1

        manager, messages = _make_run_manager(conn)
        result = run_pending_plans(manager, "alice")

        assert len(result["done"]) == 1
        assert len(result["failed"]) == 0

        # plan は done になっている
        cur = conn.execute(
            "SELECT status FROM curation_plans WHERE op_id = ?",
            ("merge:memopedia:1+memopedia:2",),
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
        enqueue_plan(conn, kind="merge", op_id="merge:no_such+memopedia:2", refs=["memopedia:999", "memopedia:2"])
        # 2番目: 正常
        enqueue_plan(conn, kind="merge", op_id="merge:memopedia:1+memopedia:2", refs=["memopedia:1", "memopedia:2"])

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
        enqueue_plan(conn, kind="merge", op_id="merge:memopedia:1+memopedia:2", refs=["memopedia:1", "memopedia:2"])

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


# ---------------------------------------------------------------------------
# 修正1の回帰: fold の検知 → enqueue → run_pending_plans 統合
# ---------------------------------------------------------------------------


class TestFoldIntegration:
    """fold の refs 契約 (refs=[親, 過小ページ]) が検知から実行まで通ることを固定する。

    レビュー指摘 [P1]: 検知が refs 1 件で候補を作り、実行が 2 件を要求するため
    fold は承認しても必ず失敗していた。
    """

    def test_detect_enqueue_run_fold_succeeds(self):
        from saiverse.curation import STALE_DAYS, detect_curation_candidates

        conn = _make_full_conn()
        stale = int(time.time()) - (STALE_DAYS + 1) * 86400
        _insert_page(
            conn, page_id="trunk", title="トランク", category="people",
            is_trunk=True, short_id=0,
        )
        # 実親（非 trunk）
        _insert_page(
            conn, page_id="p_parent", title="週の記録", category="people",
            content="親ページの本文" * 40, parent_id="trunk", short_id=1,
        )
        # 過小・低参照の子ページ
        _insert_page(
            conn, page_id="p_small", title="金曜日のメモ", category="people",
            content="小さなメモ", parent_id="p_parent", short_id=2,
            updated_at=stale, last_referenced_at=stale,
        )

        # 検知: fold 候補の refs は [survivor=親, absorbed=過小ページ]
        candidates = detect_curation_candidates(conn, "alice")
        fold = [c for c in candidates if c["kind"] == "fold"]
        assert len(fold) == 1, f"fold 候補が検知されない: {candidates}"
        cand = fold[0]
        assert cand["refs"] == ["memopedia:1", "memopedia:2"], (
            f"fold の refs 契約違反 (survivor=親, absorbed=過小ページ): {cand['refs']}"
        )

        # enqueue → 実行
        enqueue_plan(conn, kind=cand["kind"], op_id=cand["op_id"], refs=cand["refs"])
        manager, messages = _make_run_manager(conn)
        result = run_pending_plans(manager, "alice")

        assert result["failed"] == [], f"fold プランが失敗した: {result['report_lines']}"
        assert len(result["done"]) == 1

        from sai_memory.memopedia.storage import get_page
        # survivor=親: 過小ページの本文が親へ逐語で統合されている
        parent_after = get_page(conn, "p_parent")
        assert "小さなメモ" in parent_after.content
        assert "金曜日のメモ" in parent_after.content  # 区切り見出しに旧タイトル
        # absorbed=過小ページ: soft-delete されている
        row = conn.execute(
            "SELECT is_deleted FROM memopedia_pages WHERE id = 'p_small'"
        ).fetchone()
        assert row[0] == 1, "過小ページが soft-delete されていない"


# ---------------------------------------------------------------------------
# 修正2の回帰: 1 プラン = 1 トランザクション（失敗時に部分変更が残らない）
# ---------------------------------------------------------------------------


def _snapshot_state(conn: sqlite3.Connection) -> tuple:
    """pages（親子関係含む全列）と編集来歴のスナップショット。"""
    pages = conn.execute(
        "SELECT * FROM memopedia_pages ORDER BY id"
    ).fetchall()
    history = conn.execute(
        "SELECT * FROM memopedia_page_edit_history ORDER BY id"
    ).fetchall()
    return pages, history


class TestPlanAtomicity:
    """merge/split の各段階に例外を注入し、失敗後の pages・親子関係・編集来歴が
    プラン実行前と完全一致する（rollback される）ことを固定する。
    """

    def _setup_merge(self) -> sqlite3.Connection:
        conn = _make_full_conn()
        _insert_page(conn, page_id="root", title="root", is_trunk=True, short_id=0)
        _insert_page(
            conn, page_id="p_s", title="残る", content="本文A", parent_id="root", short_id=1,
        )
        _insert_page(
            conn, page_id="p_a", title="消える", content="本文B", parent_id="root", short_id=2,
        )
        # absorbed の子ページ（付け替えの rollback を検証するため）
        _insert_page(
            conn, page_id="child1", title="子1", content="C", parent_id="p_a", short_id=3,
        )
        enqueue_plan(conn, kind="merge", op_id="merge:memopedia:1+memopedia:2", refs=["memopedia:1", "memopedia:2"])
        return conn

    def test_merge_failure_at_last_stage_rolls_back_everything(self):
        """最終段階 (absorbed soft-delete) の失敗で、それ以前の変更
        （子の付け替え・survivor 本文更新・編集来歴）が全て巻き戻る。"""
        from sai_memory.memopedia.core import Memopedia

        conn = self._setup_merge()
        before = _snapshot_state(conn)
        manager, messages = _make_run_manager(conn)

        with patch.object(
            Memopedia, "delete_page", side_effect=RuntimeError("boom: delete"),
        ):
            result = run_pending_plans(manager, "alice")

        assert len(result["failed"]) == 1
        assert result["done"] == []
        assert _snapshot_state(conn) == before, "失敗後に部分変更が残っている"
        # 翌朝報告の「ページは変更されていません」が事実と一致する
        assert any(
            "ページは変更されていません" in line for line in result["report_lines"]
        )

    def test_merge_failure_at_survivor_update_rolls_back(self):
        """中間段階 (survivor 本文更新) の失敗でも、先行する子の付け替えが巻き戻る。"""
        from sai_memory.memopedia.core import Memopedia

        conn = self._setup_merge()
        before = _snapshot_state(conn)
        manager, messages = _make_run_manager(conn)

        with patch.object(
            Memopedia, "update_page", side_effect=RuntimeError("boom: update"),
        ):
            result = run_pending_plans(manager, "alice")

        assert len(result["failed"]) == 1
        assert _snapshot_state(conn) == before, (
            "子の付け替え (move_pages_to_parent) が rollback されていない"
        )

    def test_merge_success_commits(self):
        """（対照）正常系は commit され、変更が確定して残る。"""
        conn = self._setup_merge()
        before = _snapshot_state(conn)
        manager, messages = _make_run_manager(conn)
        result = run_pending_plans(manager, "alice")
        assert len(result["done"]) == 1
        assert _snapshot_state(conn) != before

    # ----- split -----

    def _setup_split(self) -> sqlite3.Connection:
        conn = _make_full_conn()
        _insert_page(conn, page_id="root", title="root", is_trunk=True, short_id=0)
        _insert_page(
            conn, page_id="target", title="大きなページ",
            content="ブロック0\n\nブロック1\n\nブロック2",
            parent_id="root", short_id=1,
        )
        enqueue_plan(conn, kind="split", op_id="split:memopedia:1", refs=["memopedia:1"])
        return conn

    def _run_with_mock_llm(self, manager: Any, llm_response: Dict[str, Any]) -> Dict[str, Any]:
        """run_pending_plans の LLM クライアント取得経路を mock で固定する。"""
        mock_llm = _make_mock_llm(llm_response)
        with patch(
            "saiverse.model_configs.find_model_config",
            return_value=(
                "mock-model",
                {"model": "mock-model", "context_length": 1000, "provider": "mock"},
            ),
        ), patch(
            "llm_clients.factory.get_llm_client",
            return_value=mock_llm,
        ):
            return run_pending_plans(manager, "alice")

    def test_split_failure_on_second_child_rolls_back_first_child(self):
        """2 枚目の子ページ作成の失敗で、作成済みの 1 枚目も巻き戻る。"""
        from sai_memory.memopedia.core import Memopedia

        conn = self._setup_split()
        before = _snapshot_state(conn)
        manager, messages = _make_run_manager(conn)

        orig_create = Memopedia.create_page
        calls = {"n": 0}

        def flaky_create(self, **kwargs):
            calls["n"] += 1
            if calls["n"] >= 2:
                raise RuntimeError("boom: 2nd child")
            return orig_create(self, **kwargs)

        llm_response = {
            "sections": [
                {"title": "子A", "block_indices": [0]},
                {"title": "子B", "block_indices": [1]},
            ],
            "remaining_block_indices": [2],
        }
        with patch.object(Memopedia, "create_page", flaky_create):
            result = self._run_with_mock_llm(manager, llm_response)

        assert calls["n"] == 2, "2枚目の作成まで到達していない"
        assert len(result["failed"]) == 1
        assert _snapshot_state(conn) == before, "1枚目の子ページが残っている"

    def test_split_failure_on_parent_update_rolls_back_children(self):
        """最終段階 (親の残り本文更新) の失敗で、作成済みの子ページが全て巻き戻る。"""
        from sai_memory.memopedia.core import Memopedia

        conn = self._setup_split()
        before = _snapshot_state(conn)
        manager, messages = _make_run_manager(conn)

        llm_response = {
            "sections": [{"title": "子A", "block_indices": [0, 1]}],
            "remaining_block_indices": [2],
        }
        with patch.object(
            Memopedia, "update_page", side_effect=RuntimeError("boom: parent update"),
        ):
            result = self._run_with_mock_llm(manager, llm_response)

        assert len(result["failed"]) == 1
        assert _snapshot_state(conn) == before, "作成済み子ページが残っている"


# ---------------------------------------------------------------------------
# 修正3の回帰: split の文字レベル本文保存則
# ---------------------------------------------------------------------------


class TestSplitLossless:
    """split 後に「子ページ全本文＋親の残り本文」から元本文が文字レベルで
    完全一致で復元できることを固定する（intent の本文保存則が正）。
    """

    _GUIDE_MARKER = "\n\n## 分割された節"

    def _split_and_reconstruct(
        self, content: str, llm_response: Dict[str, Any],
    ) -> tuple:
        """execute_split を実行し、(子ページ list, 親の残り本文) を返す。"""
        conn = _make_full_conn()
        _insert_page(conn, page_id="root", title="root", is_trunk=True, short_id=0)
        _insert_page(
            conn, page_id="target", title="対象ページ",
            content=content, parent_id="root", short_id=1,
        )
        memopedia = _make_memopedia_stub(conn)
        llm = _make_mock_llm(llm_response)
        execute_split(conn, "target", memopedia, llm)

        from sai_memory.memopedia.storage import get_children, get_page
        children = get_children(conn, "target")
        parent = get_page(conn, "target")
        idx = parent.content.find(self._GUIDE_MARKER)
        assert idx >= 0, f"親に子への導線が無い: {parent.content!r}"
        remaining_out = parent.content[:idx]
        return children, remaining_out

    def test_review_minimal_repro_is_preserved(self):
        """レビュー指摘の最小再現: 先頭末尾空白・3連改行・末尾改行を含む本文。"""
        content = "  A  \n\n\nB  \n"
        children, remaining_out = self._split_and_reconstruct(content, {
            "sections": [{"title": "子A", "block_indices": [0]}],
            "remaining_block_indices": [1],
        })
        assert len(children) == 1
        reconstructed = children[0].content + remaining_out
        assert reconstructed == content, (
            f"本文保存則違反: {reconstructed!r} != {content!r}"
        )

    def test_three_blocks_exact_reconstruction(self):
        """複数子ページ＋残りの構成でも文字レベルで完全一致する。"""
        content = "  冒頭A  \n\n\n中間B  \n\n末尾C  \n"
        children, remaining_out = self._split_and_reconstruct(content, {
            "sections": [
                {"title": "子A", "block_indices": [0]},
                {"title": "子B", "block_indices": [1]},
            ],
            "remaining_block_indices": [2],
        })
        assert len(children) == 2
        child_a = next(c for c in children if c.title == "子A")
        child_b = next(c for c in children if c.title == "子B")
        reconstructed = child_a.content + child_b.content + remaining_out
        assert reconstructed == content, (
            f"本文保存則違反: {reconstructed!r} != {content!r}"
        )


class TestSplitRealWorldResponse:
    """実機で保存則違反として棄却された LLM 応答（2026-07-14 未明、aifi_city_a）の回帰。

    memopedia:25「まはー」(194 ブロック) に対し gemini-3.5-flash-paid が返した割当を
    逐語で再現する。旧実装はこれを「重複43・欠落27」として棄却していた
    （元の応答は logs/20260714_003747/llm_io.log）。正規化後はそのまま受理され、
    競合・非申告のブロックが親に残ることで保存則が成立する。
    """

    # 実応答の block_indices（7 セクション）。remaining は当時 [16] を返していたが、
    # 現行スキーマでは受け取らない（補集合として導出する）。
    REAL_SECTIONS = [
        ("恋愛観とAIへの感情",
         [0, 1, 2, 3, 4, 5, 8, 9, 10, 11, 12, 13, 14, 15, 23, 30, 32, 33, 41, 43, 44,
          48, 71, 72, 74, 85, 87, 90, 115, 116, 122, 136, 139, 144, 145, 153]),
        ("AIの技術的検証と自我の研究",
         [6, 7, 8, 17, 18, 19, 20, 21, 24, 25, 26, 27, 28, 29, 31, 33, 34, 35, 57, 58,
          59, 60, 63, 64, 73, 76, 77, 79, 80, 81, 82, 89, 93, 94, 95, 97, 98, 99, 101,
          103, 104, 107, 109, 114, 116, 117, 120, 121, 124, 128, 129, 133, 137, 138,
          139, 142, 143, 146, 147, 150, 151, 154, 157, 158, 160, 162, 164, 172, 173,
          180, 183, 184, 186, 187, 188, 189, 190, 191]),
        ("アイフィのビジュアルと創作支援",
         [61, 62, 105, 106, 110, 111, 112, 113, 118, 119, 121, 123, 126, 132, 144, 153,
          155, 156, 164, 165, 166, 169, 172, 173, 181, 193]),
        ("SAIVerseの開発と展開",
         [124, 126, 140, 141, 157, 158, 159, 162, 163, 164, 167, 168, 169, 170, 172,
          173, 175, 176, 180, 186, 188, 189, 190, 191]),
        ("個人の特性と社会生活",
         [37, 38, 41, 51, 54, 85, 91, 93, 96, 97, 100, 108, 138, 148, 171, 174, 176,
          177, 178, 179, 181, 182, 184, 192]),
        ("トラブル対応とアカウント管理",
         [65, 66, 67, 68, 69, 70, 71, 148, 151, 152, 161, 182, 189]),
        ("日常と趣味",
         [39, 40, 56, 101, 165, 168, 174, 175, 176, 177, 179, 181, 182, 192, 193]),
    ]
    TOTAL_BLOCKS = 194

    def _run(self):
        content = "\n\n".join(f"ブロック{i}の本文" for i in range(self.TOTAL_BLOCKS))
        conn = _make_full_conn()
        _insert_page(
            conn, page_id="target", title="まはー",
            content=content, parent_id="root", short_id=25,
        )
        memopedia = _make_memopedia_stub(conn)
        llm = _make_mock_llm({
            "sections": [
                {"title": t, "block_indices": idx} for t, idx in self.REAL_SECTIONS
            ],
        })
        result = execute_split(conn, "target", memopedia, llm)
        return conn, content, result

    def test_real_response_is_accepted_not_rejected(self):
        """旧実装が棄却した応答が、棄却されずに適用される。"""
        conn, content, result = self._run()
        assert result["total_blocks"] == self.TOTAL_BLOCKS
        assert len(result["sections"]) == len(self.REAL_SECTIONS)

    @staticmethod
    def _block_numbers(text: str) -> List[int]:
        import re
        return [int(m) for m in re.findall(r"ブロック(\d+)の本文", text)]

    def test_real_response_preserves_content_verbatim(self):
        """子ページ全部＋親の残り＝元の全ブロック（各ブロックがちょうど一度）。"""
        conn, content, result = self._run()
        from sai_memory.memopedia.storage import get_page, get_children

        seen: List[int] = []
        for child in get_children(conn, "target"):
            seen += self._block_numbers(child.content)
        seen += self._block_numbers(get_page(conn, "target").content)

        assert sorted(seen) == list(range(self.TOTAL_BLOCKS)), (
            "ブロックの取りこぼし／複製がある"
        )

    def test_real_response_children_keep_index_order(self):
        """各子ページの本文はブロック index 昇順（原文の順序が保たれる）。"""
        conn, content, result = self._run()
        from sai_memory.memopedia.storage import get_children

        for child in get_children(conn, "target"):
            nums = self._block_numbers(child.content)
            assert nums == sorted(nums), f"{child.title}: 順序が崩れている {nums}"

    def test_conflicting_and_unclaimed_blocks_go_to_parent(self):
        """複数セクションが挙げたブロック・どこにも挙がらなかったブロックが親に残る。"""
        conn, content, result = self._run()
        from collections import Counter

        counter = Counter()
        for _, idx in self.REAL_SECTIONS:
            counter.update(set(idx))  # 同一セクション内の重複は競合ではない
        conflicting = {i for i, n in counter.items() if n > 1}
        unclaimed = set(range(self.TOTAL_BLOCKS)) - set(counter)
        assert conflicting, "この回帰データは競合を含む前提"
        assert unclaimed, "この回帰データは非申告ブロックを含む前提"

        from sai_memory.memopedia.storage import get_page
        parent_blocks = set(get_page(conn, "target").content.split("\n\n"))
        for i in conflicting | unclaimed:
            assert f"ブロック{i}の本文" in parent_blocks, f"ブロック{i} が親に残っていない"
        assert result["remaining_block_count"] == len(conflicting | unclaimed)

    def test_no_block_is_duplicated_across_children(self):
        """同じブロックが 2 つの子ページに実体を持たない（本文の複製禁止）。"""
        conn, content, result = self._run()
        from sai_memory.memopedia.storage import get_children

        seen: set = set()
        for child in get_children(conn, "target"):
            for n in self._block_numbers(child.content):
                assert n not in seen, f"複製されたブロック: {n}"
                seen.add(n)


class TestSplitChildSummary:
    """子ページの概要は分割と同じ構造化出力で受け取り、子ページへ渡す。

    実機 2026-07-14 の編纂で生まれた子ページは summary が空文字だった
    （create_page は受け取れるのに apply_split が渡していなかった）。
    """

    def _setup(self, content: str):
        conn = _make_full_conn()
        _insert_page(
            conn, page_id="target", title="大きなページ",
            content=content, parent_id="root", short_id=1,
        )
        return conn, _make_memopedia_stub(conn)

    def test_declared_summary_reaches_child_page(self):
        conn, memopedia = self._setup("A\n\nB\n\nC")
        llm = _make_mock_llm({
            "child_pages": [
                {"title": "子A", "summary": "Aについての概要"},
                {"title": "子B", "summary": "Bについての概要"},
            ],
            "sections": [
                {"title": "子A", "block_indices": [0]},
                {"title": "子B", "block_indices": [1]},
            ],
        })

        execute_split(conn, "target", memopedia, llm)

        from sai_memory.memopedia.storage import get_children
        children = {c.title: c.summary for c in get_children(conn, "target")}
        assert children["子A"] == "Aについての概要"
        assert children["子B"] == "Bについての概要"

    def test_missing_child_pages_falls_back_to_empty_summary(self):
        """child_pages が無い応答でも分割は成立する（概要は空）。"""
        conn, memopedia = self._setup("A\n\nB\n\nC")
        llm = _make_mock_llm({
            "sections": [{"title": "子A", "block_indices": [0, 1]}],
        })

        execute_split(conn, "target", memopedia, llm)

        from sai_memory.memopedia.storage import get_children
        children = get_children(conn, "target")
        assert len(children) == 1
        assert children[0].summary == ""

    def test_summary_is_matched_by_title_not_position(self):
        """概要はタイトルで引く（sections の並びが宣言と違っても取り違えない）。"""
        conn, memopedia = self._setup("A\n\nB\n\nC")
        llm = _make_mock_llm({
            "child_pages": [
                {"title": "子A", "summary": "Aの概要"},
                {"title": "子B", "summary": "Bの概要"},
            ],
            # 宣言と逆順で割り当てる
            "sections": [
                {"title": "子B", "block_indices": [1]},
                {"title": "子A", "block_indices": [0]},
            ],
        })

        execute_split(conn, "target", memopedia, llm)

        from sai_memory.memopedia.storage import get_children
        children = {c.title: c.summary for c in get_children(conn, "target")}
        assert children["子A"] == "Aの概要"
        assert children["子B"] == "Bの概要"
