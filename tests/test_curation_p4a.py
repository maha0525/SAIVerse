"""P4-a 編纂検知・裁定・プラン永続化のテスト。

検証対象:
- 肥大検知 (content > OVERSIZED_THRESHOLD → split 候補)
- 過小検知 (content < UNDERSIZED_THRESHOLD + 低参照 + 実親あり → fold 候補)
- trunk 直下の過小ページは候補にならない
- 類似検知 (キーワード共起 >= SIMILAR_MIN_KEYWORDS → merge 候補、残す側=古い方)
- metabolizable 外カテゴリ (theme/core) は対象外
- 最大 3 件
- 決定論 (同入力 → 同出力)
- build_day_close_schema: 候補ありで curation_reviews フィールドが出る
- build_day_close_schema: 候補ゼロでフィールドが無い
- judgment_finalize: approve → curation_plans に pending 行、skip → 行なし
- judgment_finalize: 重複 approve → 行は 1 件のまま
- 閾値一元化: memopedia_health / maintain_memopedia が curation.py の定数を参照
"""
from __future__ import annotations

import json
import sqlite3
import time
from types import SimpleNamespace
from typing import Any, Dict, List
import pytest

from saiverse.curation import (
    MAX_CANDIDATES,
    OVERSIZED_THRESHOLD,
    SIMILAR_MIN_KEYWORDS,
    STALE_DAYS,
    UNDERSIZED_THRESHOLD,
    detect_curation_candidates,
)
from sai_memory.curation_ops import (
    enqueue_plan,
    init_curation_tables,
    list_pending,
)


# ---------------------------------------------------------------------------
# helpers: in-memory DB を直接操作する最小ツール
# ---------------------------------------------------------------------------


def _make_conn() -> sqlite3.Connection:
    """テスト用の in-memory memory.db（memopedia_pages だけ）。"""
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.execute(
        """
        CREATE TABLE memopedia_pages (
            id          TEXT PRIMARY KEY,
            parent_id   TEXT,
            title       TEXT NOT NULL,
            summary     TEXT DEFAULT '',
            content     TEXT DEFAULT '',
            category    TEXT NOT NULL,
            created_at  INTEGER NOT NULL,
            updated_at  INTEGER NOT NULL,
            keywords    TEXT DEFAULT '[]',
            metadata    TEXT,
            is_deleted  INTEGER DEFAULT 0,
            vividness   TEXT DEFAULT 'rough',
            is_trunk    INTEGER DEFAULT 0,
            is_important INTEGER DEFAULT 0,
            last_referenced_at INTEGER,
            short_id    INTEGER
        )
        """
    )
    conn.commit()
    return conn


_NEXT_SHORT_ID = [1]


def _insert_page(
    conn: sqlite3.Connection,
    *,
    page_id: str,
    title: str,
    category: str,
    content: str = "",
    parent_id: str = None,
    keywords: List[str] = None,
    is_trunk: bool = False,
    is_deleted: bool = False,
    created_at: int = None,
    updated_at: int = None,
    last_referenced_at: int = None,
    short_id: int = None,
) -> None:
    now = int(time.time())
    if short_id is None:
        short_id = _NEXT_SHORT_ID[0]
        _NEXT_SHORT_ID[0] += 1
    conn.execute(
        """
        INSERT INTO memopedia_pages
            (id, parent_id, title, category, content, keywords,
             is_trunk, is_deleted, created_at, updated_at,
             last_referenced_at, short_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            page_id,
            parent_id,
            title,
            category,
            content,
            json.dumps(keywords or [], ensure_ascii=False),
            1 if is_trunk else 0,
            1 if is_deleted else 0,
            created_at if created_at is not None else now - 3600,
            updated_at if updated_at is not None else now - 3600,
            last_referenced_at,
            short_id,
        ),
    )
    conn.commit()


def _stale_ts() -> int:
    """STALE_DAYS + 1 日前の epoch 秒（確実に「低参照」と判定される）。"""
    return int(time.time()) - (STALE_DAYS + 1) * 86400


def _fresh_ts() -> int:
    """1日前の epoch 秒（「低参照」にならない）。"""
    return int(time.time()) - 86400


# ---------------------------------------------------------------------------
# detect_curation_candidates
# ---------------------------------------------------------------------------


class TestDetectOversized:
    def test_oversized_page_becomes_split_candidate(self):
        conn = _make_conn()
        # trunk（トランク）
        _insert_page(conn, page_id="root_people", title="人物", category="people", is_trunk=True, short_id=0)
        # 肥大ページ: content が閾値を超える
        big_content = "a" * (OVERSIZED_THRESHOLD + 100)
        _insert_page(
            conn, page_id="p1", title="技術の記録", category="people",
            content=big_content, parent_id="root_people", short_id=1,
        )
        candidates = detect_curation_candidates(conn, "alice")
        assert len(candidates) == 1
        c = candidates[0]
        assert c["kind"] == "split"
        assert "m:1" in c["op_id"]
        assert "[肥大]" in c["line"]
        assert "分割" in c["line"]

    def test_normal_size_page_not_candidate(self):
        conn = _make_conn()
        _insert_page(conn, page_id="root_people", title="人物", category="people", is_trunk=True, short_id=0)
        _insert_page(
            conn, page_id="p1", title="普通のページ", category="people",
            content="a" * 100, parent_id="root_people", short_id=1,
        )
        candidates = detect_curation_candidates(conn, "alice")
        assert candidates == []

    def test_trunk_is_excluded_from_oversized(self):
        conn = _make_conn()
        big_content = "a" * (OVERSIZED_THRESHOLD + 100)
        _insert_page(
            conn, page_id="root_people", title="人物", category="people",
            content=big_content, is_trunk=True, short_id=0,
        )
        candidates = detect_curation_candidates(conn, "alice")
        assert candidates == []

    def test_deleted_page_excluded(self):
        conn = _make_conn()
        _insert_page(conn, page_id="root_people", title="人物", category="people", is_trunk=True, short_id=0)
        big_content = "a" * (OVERSIZED_THRESHOLD + 100)
        _insert_page(
            conn, page_id="p1", title="削除済み", category="people",
            content=big_content, parent_id="root_people", is_deleted=True, short_id=1,
        )
        candidates = detect_curation_candidates(conn, "alice")
        assert candidates == []


class TestDetectUndersized:
    def test_small_stale_page_with_real_parent_is_fold_candidate(self):
        conn = _make_conn()
        stale = _stale_ts()
        # trunk
        _insert_page(conn, page_id="root_people", title="人物", category="people", is_trunk=True, short_id=0)
        # 非 trunk の親
        _insert_page(
            conn, page_id="p_parent", title="週の記録", category="people",
            content="中程度の内容", parent_id="root_people", short_id=1,
            updated_at=stale, last_referenced_at=stale,
        )
        # 過小・低参照の子ページ
        _insert_page(
            conn, page_id="p_small", title="金曜日のメモ", category="people",
            content="a" * (UNDERSIZED_THRESHOLD - 1),
            parent_id="p_parent", short_id=2,
            updated_at=stale, last_referenced_at=stale,
        )
        candidates = detect_curation_candidates(conn, "alice")
        fold_candidates = [c for c in candidates if c["kind"] == "fold"]
        assert len(fold_candidates) == 1
        c = fold_candidates[0]
        assert "m:2" in c["op_id"]
        assert "[過小]" in c["line"]
        assert "統合" in c["line"]
        # 実行契約 (run_pending_plans): refs[0]=survivor(親), refs[1]=absorbed(過小ページ)
        assert c["refs"] == ["m:1", "m:2"], (
            f"fold の refs は [親, 過小ページ] の 2 件が必要: {c['refs']}"
        )

    def test_trunk_direct_child_not_undersized_candidate(self):
        """trunk 直下の過小ページは候補にしない（決定論の統合先が無い）。"""
        conn = _make_conn()
        stale = _stale_ts()
        _insert_page(conn, page_id="root_people", title="人物", category="people", is_trunk=True, short_id=0)
        _insert_page(
            conn, page_id="p_small", title="孤立した小ページ", category="people",
            content="a" * (UNDERSIZED_THRESHOLD - 1),
            parent_id="root_people", short_id=1,  # trunk が親
            updated_at=stale, last_referenced_at=stale,
        )
        candidates = detect_curation_candidates(conn, "alice")
        fold_candidates = [c for c in candidates if c["kind"] == "fold"]
        assert fold_candidates == []

    def test_fresh_small_page_not_candidate(self):
        """最近参照されたページは過小でも候補にしない。"""
        conn = _make_conn()
        fresh = _fresh_ts()
        _insert_page(conn, page_id="root_people", title="人物", category="people", is_trunk=True, short_id=0)
        _insert_page(
            conn, page_id="p_parent", title="親ページ", category="people",
            content="十分な内容のある親ページ", parent_id="root_people", short_id=1,
        )
        _insert_page(
            conn, page_id="p_small", title="新鮮な小ページ", category="people",
            content="a" * (UNDERSIZED_THRESHOLD - 1),
            parent_id="p_parent", short_id=2,
            updated_at=fresh, last_referenced_at=fresh,
        )
        candidates = detect_curation_candidates(conn, "alice")
        fold_candidates = [c for c in candidates if c["kind"] == "fold"]
        assert fold_candidates == []


class TestDetectSimilar:
    def test_keyword_overlap_produces_merge_candidate(self):
        conn = _make_conn()
        kws = ["SEA", "Playbook", "City", "Persona"]
        _insert_page(conn, page_id="root_terms", title="用語", category="terms", is_trunk=True, short_id=0)
        _insert_page(
            conn, page_id="p_a", title="SAIVerse", category="terms",
            content="content a", keywords=kws,
            parent_id="root_terms", short_id=1,
            created_at=1000, updated_at=1000,
        )
        _insert_page(
            conn, page_id="p_b", title="SAIVerseの構造", category="terms",
            content="content b", keywords=kws,
            parent_id="root_terms", short_id=2,
            created_at=2000, updated_at=2000,
        )
        candidates = detect_curation_candidates(conn, "alice")
        merge_candidates = [c for c in candidates if c["kind"] == "merge"]
        assert len(merge_candidates) == 1
        c = merge_candidates[0]
        assert "[類似]" in c["line"]
        assert "統合" in c["line"]
        # 残す側 = 古い方 = m:1
        assert "m:1" in c["refs"]
        assert "m:2" in c["refs"]
        # op_id は古い方が先
        assert c["op_id"].startswith("merge:m:1")

    def test_title_inclusion_produces_merge_candidate(self):
        conn = _make_conn()
        _insert_page(conn, page_id="root_terms", title="用語", category="terms", is_trunk=True, short_id=0)
        # title_b が title_a に包含されている
        _insert_page(
            conn, page_id="p_a", title="SAIVerse機能一覧", category="terms",
            content="content a",
            parent_id="root_terms", short_id=1,
            created_at=1000, updated_at=1000,
        )
        _insert_page(
            conn, page_id="p_b", title="SAIVerse", category="terms",
            content="content b",
            parent_id="root_terms", short_id=2,
            created_at=2000, updated_at=2000,
        )
        candidates = detect_curation_candidates(conn, "alice")
        merge_candidates = [c for c in candidates if c["kind"] == "merge"]
        assert len(merge_candidates) == 1

    def test_different_category_not_similar(self):
        """異カテゴリはキーワード共起があっても類似にしない。"""
        conn = _make_conn()
        kws = ["SEA", "Playbook", "City", "Persona"]
        _insert_page(conn, page_id="root_terms", title="用語", category="terms", is_trunk=True, short_id=0)
        _insert_page(conn, page_id="root_people", title="人物", category="people", is_trunk=True, short_id=100)
        _insert_page(
            conn, page_id="p_a", title="用語A", category="terms",
            content="content a", keywords=kws,
            parent_id="root_terms", short_id=1,
        )
        _insert_page(
            conn, page_id="p_b", title="人物B", category="people",
            content="content b", keywords=kws,
            parent_id="root_people", short_id=2,
        )
        candidates = detect_curation_candidates(conn, "alice")
        merge_candidates = [c for c in candidates if c["kind"] == "merge"]
        assert merge_candidates == []

    def test_insufficient_keyword_overlap(self):
        conn = _make_conn()
        _insert_page(conn, page_id="root_terms", title="用語", category="terms", is_trunk=True, short_id=0)
        _insert_page(
            conn, page_id="p_a", title="ページA", category="terms",
            content="content a", keywords=["SEA", "City"],
            parent_id="root_terms", short_id=1,
        )
        _insert_page(
            conn, page_id="p_b", title="ページB", category="terms",
            content="content b", keywords=["SEA", "Playbook"],
            parent_id="root_terms", short_id=2,
        )
        # 共起は1語（SIMILAR_MIN_KEYWORDS=3 未満）
        candidates = detect_curation_candidates(conn, "alice")
        merge_candidates = [c for c in candidates if c["kind"] == "merge"]
        assert merge_candidates == []


class TestCategoryFiltering:
    def test_theme_category_excluded(self):
        """theme カテゴリは metabolizable=False なので対象外。"""
        conn = _make_conn()
        big_content = "a" * (OVERSIZED_THRESHOLD + 100)
        _insert_page(conn, page_id="root_theme", title="テーマ", category="theme", is_trunk=True, short_id=0)
        _insert_page(
            conn, page_id="p_theme", title="テーマページ", category="theme",
            content=big_content, parent_id="root_theme", short_id=1,
        )
        candidates = detect_curation_candidates(conn, "alice")
        assert candidates == []

    def test_core_category_excluded(self):
        """core カテゴリは metabolizable=False なので対象外。"""
        conn = _make_conn()
        big_content = "a" * (OVERSIZED_THRESHOLD + 100)
        _insert_page(conn, page_id="root_core", title="コア記憶", category="core", is_trunk=True, short_id=0)
        _insert_page(
            conn, page_id="p_core", title="コア記憶ページ", category="core",
            content=big_content, parent_id="root_core", short_id=1,
        )
        candidates = detect_curation_candidates(conn, "alice")
        assert candidates == []

    def test_people_terms_plans_events_are_included(self):
        """people/terms/plans/events は metabolizable=True なので対象になる。"""
        conn = _make_conn()
        big_content = "a" * (OVERSIZED_THRESHOLD + 100)
        for i, cat in enumerate(["people", "terms", "plans", "events"]):
            _insert_page(
                conn, page_id=f"root_{cat}", title=cat, category=cat,
                is_trunk=True, short_id=100 + i,
            )
            _insert_page(
                conn, page_id=f"p_{cat}", title=f"{cat}ページ", category=cat,
                content=big_content, parent_id=f"root_{cat}",
                short_id=i + 1,
                created_at=1000 * (i + 1), updated_at=1000 * (i + 1),
            )
        candidates = detect_curation_candidates(conn, "alice")
        # MAX_CANDIDATES に切られる
        assert len(candidates) == MAX_CANDIDATES
        kinds = [c["kind"] for c in candidates]
        assert all(k == "split" for k in kinds)


class TestMaxCandidates:
    def test_max_candidates_capped(self):
        """候補は MAX_CANDIDATES 件に切られる。"""
        conn = _make_conn()
        big_content = "a" * (OVERSIZED_THRESHOLD + 100)
        for i in range(MAX_CANDIDATES + 2):
            cat = "people"
            _insert_page(
                conn, page_id=f"root_people_{i}", title="人物", category=cat,
                is_trunk=True, short_id=100 + i,
            )
            _insert_page(
                conn, page_id=f"p_{i}", title=f"ページ{i}", category=cat,
                content=big_content * (i + 1),
                parent_id=f"root_people_{i}", short_id=i + 1,
                created_at=1000 * (i + 1), updated_at=1000 * (i + 1),
            )
        candidates = detect_curation_candidates(conn, "alice")
        assert len(candidates) <= MAX_CANDIDATES


class TestDeterminism:
    def test_same_input_same_output(self):
        """同じ DB 状態からは同じ候補が返る（決定論）。"""
        conn = _make_conn()
        big_content = "a" * (OVERSIZED_THRESHOLD + 100)
        _insert_page(conn, page_id="root_people", title="人物", category="people", is_trunk=True, short_id=0)
        _insert_page(
            conn, page_id="p1", title="技術の記録", category="people",
            content=big_content, parent_id="root_people", short_id=1,
        )
        r1 = detect_curation_candidates(conn, "alice")
        r2 = detect_curation_candidates(conn, "alice")
        assert r1 == r2


# ---------------------------------------------------------------------------
# build_day_close_schema の curation_reviews フィールド
# ---------------------------------------------------------------------------


class TestDayCloseSchema:
    """judgment_points.build_day_close_schema の curation_reviews テスト。"""

    def _make_manager(self):
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker
        from sqlalchemy.pool import StaticPool
        from database.models import Base
        engine = create_engine(
            "sqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(engine)
        Session = sessionmaker(bind=engine)
        return SimpleNamespace(
            SessionLocal=Session,
            personas={},
            event_scheduler=None,
            track_manager=None,
            buildings=[],
        )

    def test_schema_has_curation_reviews_when_candidates_exist(self):
        from saiverse import judgment_points as jp

        manager = self._make_manager()
        candidates = [
            {
                "op_id": "split:m:1",
                "kind": "split",
                "refs": ["m:1"],
                "line": "[肥大] m:1「大きいページ」 5,100字 — 子ページへの分割を提案",
            }
        ]
        schema = jp.build_day_close_schema(
            manager=manager,
            persona_id="alice",
            touched_desire_refs=[],
            curation_candidates=candidates,
        )
        assert "curation_reviews" in schema["properties"]
        cr = schema["properties"]["curation_reviews"]
        # op_id の enum に候補の op_id が含まれる
        item_props = cr["items"]["properties"]
        assert "split:m:1" in item_props["op_id"]["enum"]
        assert set(item_props["verdict"]["enum"]) == {"approve", "skip"}

    def test_schema_has_no_curation_reviews_when_no_candidates(self):
        from saiverse import judgment_points as jp

        manager = self._make_manager()
        schema = jp.build_day_close_schema(
            manager=manager,
            persona_id="alice",
            touched_desire_refs=[],
            curation_candidates=[],
        )
        assert "curation_reviews" not in schema["properties"]

    def test_schema_has_no_curation_reviews_when_none(self):
        from saiverse import judgment_points as jp

        manager = self._make_manager()
        schema = jp.build_day_close_schema(
            manager=manager,
            persona_id="alice",
            touched_desire_refs=[],
            curation_candidates=None,
        )
        assert "curation_reviews" not in schema["properties"]


# ---------------------------------------------------------------------------
# judgment_finalize の curation_reviews 適用
# ---------------------------------------------------------------------------


@pytest.fixture
def mem_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    init_curation_tables(conn)
    return conn


def _make_finalize_manager(conn: sqlite3.Connection):
    """judgment_finalize の _apply_curation_reviews が触る最小スタブ。"""
    fake_adapter = SimpleNamespace(conn=conn)
    persona_obj = SimpleNamespace(sai_memory=fake_adapter)
    return SimpleNamespace(
        personas={"alice": persona_obj},
        SessionLocal=None,
    )


class TestCurationReviewsFinalize:
    def _run_finalize(self, mem_conn, output, curation_candidates):
        """_apply_curation_reviews を直接呼ぶヘルパ。"""
        from builtin_data.tools.judgment_finalize import _apply_curation_reviews

        manager = _make_finalize_manager(mem_conn)
        ctx = {"curation_candidates": curation_candidates}
        lines: List[str] = []
        warnings: List[str] = []
        applied = _apply_curation_reviews(
            manager=manager,
            persona_id="alice",
            output=output,
            ctx=ctx,
            lines=lines,
            warnings=warnings,
        )
        return applied, lines, warnings

    def test_approve_creates_pending_plan(self, mem_conn):
        candidates = [
            {
                "op_id": "split:m:5",
                "kind": "split",
                "refs": ["m:5"],
                "line": "[肥大] m:5「技術の記録」 5,100字 — 子ページへの分割を提案",
            }
        ]
        output = {
            "curation_reviews": [
                {"op_id": "split:m:5", "verdict": "approve"},
            ]
        }
        applied, lines, warnings = self._run_finalize(mem_conn, output, candidates)
        assert applied is True
        assert warnings == []
        pending = list_pending(mem_conn)
        assert len(pending) == 1
        assert pending[0]["op_id"] == "split:m:5"
        assert pending[0]["kind"] == "split"
        # list_pending は WHERE status='pending' で絞り込み済みなので
        # status キーは返さない設計。存在を確認するには件数で十分。

    def test_skip_creates_no_plan(self, mem_conn):
        candidates = [
            {
                "op_id": "fold:m:12",
                "kind": "fold",
                "refs": ["m:12"],
                "line": "[過小] m:12「金曜日のメモ」 60字 — 統合を提案",
            }
        ]
        output = {
            "curation_reviews": [
                {"op_id": "fold:m:12", "verdict": "skip"},
            ]
        }
        applied, lines, warnings = self._run_finalize(mem_conn, output, candidates)
        assert applied is False
        assert list_pending(mem_conn) == []

    def test_duplicate_approve_creates_one_plan(self, mem_conn):
        """同じ op_id の approve を2度送っても pending 行は 1 件のまま。"""
        candidates = [
            {
                "op_id": "merge:m:1+m:2",
                "kind": "merge",
                "refs": ["m:1", "m:2"],
                "line": "[類似] ...",
            }
        ]
        output = {
            "curation_reviews": [
                {"op_id": "merge:m:1+m:2", "verdict": "approve"},
            ]
        }
        # 1 回目
        self._run_finalize(mem_conn, output, candidates)
        # 2 回目（同じ op_id で approve）
        applied, lines, warnings = self._run_finalize(mem_conn, output, candidates)
        # 2 回目は enqueue_plan が重複をスキップする（applied=True だが行は増えない）
        pending = list_pending(mem_conn)
        assert len(pending) == 1

    def test_invalid_op_id_rejected(self, mem_conn):
        candidates = [
            {
                "op_id": "split:m:5",
                "kind": "split",
                "refs": ["m:5"],
                "line": "...",
            }
        ]
        output = {
            "curation_reviews": [
                {"op_id": "no_such_op", "verdict": "approve"},
            ]
        }
        applied, lines, warnings = self._run_finalize(mem_conn, output, candidates)
        assert applied is False
        assert any("選択可能な" in w or "候補にありません" in w for w in warnings)
        assert list_pending(mem_conn) == []

    def test_no_reviews_key_returns_false(self, mem_conn):
        output = {}
        applied, lines, warnings = self._run_finalize(mem_conn, output, [])
        assert applied is False

    def test_empty_reviews_list_returns_false(self, mem_conn):
        output = {"curation_reviews": []}
        candidates = [{"op_id": "split:m:5", "kind": "split", "refs": ["m:5"], "line": "..."}]
        applied, lines, warnings = self._run_finalize(mem_conn, output, candidates)
        assert applied is False


# ---------------------------------------------------------------------------
# 閾値一元化: memopedia_health / maintain_memopedia が curation.py を参照
# ---------------------------------------------------------------------------


class TestThresholdImports:
    def test_memopedia_health_uses_curation_thresholds(self):
        from builtin_data.tools import memopedia_health as mh_mod
        from saiverse.curation import HEALTH_LARGE_THRESHOLD, HEALTH_OVERSIZED_THRESHOLD

        assert mh_mod.HEALTH_LARGE_THRESHOLD is HEALTH_LARGE_THRESHOLD
        assert mh_mod.HEALTH_OVERSIZED_THRESHOLD is HEALTH_OVERSIZED_THRESHOLD

    def test_maintain_memopedia_uses_curation_threshold(self):
        """maintain_memopedia.SPLIT_THRESHOLD は curation.OVERSIZED_THRESHOLD の alias。"""
        from scripts import maintain_memopedia as mm_mod
        from saiverse.curation import OVERSIZED_THRESHOLD

        assert mm_mod.SPLIT_THRESHOLD == OVERSIZED_THRESHOLD


# ---------------------------------------------------------------------------
# init_curation_tables / enqueue_plan / list_pending 基本動作
# ---------------------------------------------------------------------------


class TestCurationOps:
    def test_init_is_idempotent(self):
        conn = sqlite3.connect(":memory:", check_same_thread=False)
        init_curation_tables(conn)
        init_curation_tables(conn)  # 2回目も例外なし

    def test_enqueue_and_list(self):
        conn = sqlite3.connect(":memory:", check_same_thread=False)
        init_curation_tables(conn)
        plan_id = enqueue_plan(conn, kind="split", op_id="split:m:1", refs=["m:1"])
        assert isinstance(plan_id, str)
        pending = list_pending(conn)
        assert len(pending) == 1
        assert pending[0]["op_id"] == "split:m:1"
        assert pending[0]["kind"] == "split"

    def test_enqueue_idempotent_on_same_op_id(self):
        conn = sqlite3.connect(":memory:", check_same_thread=False)
        init_curation_tables(conn)
        id1 = enqueue_plan(conn, kind="merge", op_id="merge:m:1+m:2", refs=["m:1", "m:2"])
        id2 = enqueue_plan(conn, kind="merge", op_id="merge:m:1+m:2", refs=["m:1", "m:2"])
        assert id1 == id2
        assert len(list_pending(conn)) == 1
