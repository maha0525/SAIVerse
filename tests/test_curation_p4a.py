"""P4-a 編纂検知・裁定・プラン永続化のテスト。

検証対象:
- 肥大検知 (content > OVERSIZED_THRESHOLD → split 候補)
- 過小検知 (fold) は 2026-08-05 に機構ごと撤去 — 復活していないことを固定
- 類似検知 (キーワード共起 >= SIMILAR_MIN_KEYWORDS → merge 候補、残す側=古い方)
- 健全性規則 (2026-08-05): 消える側は実親も子も持たない / 統合後が肥大しない /
  分割待ちは統合に使わない / 同じ残す側は一晩 1 件
- metabolizable 外カテゴリ (theme/core) は対象外
- 最大 3 件
- 決定論 (同入力 → 同出力)
- build_day_close_schema: 候補ありで curation_reviews フィールドが出る
- build_day_close_schema: 候補ゼロでフィールドが無い
- judgment_finalize: approve → curation_plans に pending 行、skip → 行なし
- judgment_finalize: 重複 approve → 行は 1 件のまま
- 閾値一元化: memopedia_health が curation.py の定数を参照
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
    """31 日前の epoch 秒（旧・過小検知が「低参照」と判定していた古さ）。"""
    return int(time.time()) - 31 * 86400


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
        assert "memopedia:1" in c["op_id"]
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


class TestFoldRemoved:
    """過小ページを親へ畳む機構 (fold) の撤去（まはー裁定 2026-08-05）。

    統合先を親に固定した時点で対象が「実親を持つページ」に縮み、実機では
    それが全て分割の子だった＝構造的に分割の巻き戻ししかできなかった。
    経緯: docs/issues/curation_duplicate_pages_loop.md
    """

    def test_small_stale_child_produces_no_candidate(self):
        conn = _make_conn()
        stale = _stale_ts()
        _insert_page(conn, page_id="root_people", title="人物", category="people", is_trunk=True, short_id=0)
        _insert_page(
            conn, page_id="p_parent", title="週の記録", category="people",
            content="中程度の内容", parent_id="root_people", short_id=1,
            updated_at=stale, last_referenced_at=stale,
        )
        _insert_page(
            conn, page_id="p_small", title="金曜日のメモ", category="people",
            content="a" * 60, parent_id="p_parent", short_id=2,
            updated_at=stale, last_referenced_at=stale,
        )
        candidates = detect_curation_candidates(conn, "alice")
        assert [c for c in candidates if c["kind"] == "fold"] == []
        assert candidates == [], f"過小ページが何かの候補になっている: {candidates}"

    def test_fold_kind_never_emitted(self):
        """どんな組み合わせでも kind='fold' は出ない。"""
        conn = _make_conn()
        stale = _stale_ts()
        _insert_page(conn, page_id="root_people", title="人物", category="people", is_trunk=True, short_id=0)
        _insert_page(
            conn, page_id="p_parent", title="親", category="people",
            content="a" * (OVERSIZED_THRESHOLD + 10), parent_id="root_people", short_id=1,
        )
        for n in range(3):
            _insert_page(
                conn, page_id=f"p_small{n}", title=f"小ページ{n}", category="people",
                content="a" * 10, parent_id="p_parent", short_id=10 + n,
                updated_at=stale, last_referenced_at=stale,
            )
        candidates = detect_curation_candidates(conn, "alice")
        assert all(c["kind"] != "fold" for c in candidates), candidates


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
        # 残す側 = 古い方 = memopedia:1
        assert "memopedia:1" in c["refs"]
        assert "memopedia:2" in c["refs"]
        # op_id は古い方が先
        assert c["op_id"].startswith("merge:memopedia:1")

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


class TestMergeHealthRules:
    """統合候補の健全性規則（まはー裁定 2026-08-05）。

    実機 aifi_city_a で、分割が作った子が統合で親や別の人物ページへ吸い戻され、
    太った親がまた分割されて同名ページが増える輪が回っていた。
    経緯: docs/issues/curation_duplicate_pages_loop.md
    """

    def _pair(self, conn, *, a_kw=None, b_kw=None, a_content="a" * 100,
              b_content="b" * 100, a_parent="root_terms", b_parent="root_terms",
              a_title="SAIVerse機能一覧", b_title="SAIVerse"):
        """タイトル包含で類似になるペア（古い方 = a = 残す側）。"""
        _insert_page(conn, page_id="root_terms", title="用語", category="terms",
                     is_trunk=True, short_id=0)
        _insert_page(conn, page_id="p_a", title=a_title, category="terms",
                     content=a_content, keywords=a_kw, parent_id=a_parent,
                     short_id=1, created_at=1000, updated_at=1000)
        _insert_page(conn, page_id="p_b", title=b_title, category="terms",
                     content=b_content, keywords=b_kw, parent_id=b_parent,
                     short_id=2, created_at=2000, updated_at=2000)

    def _merges(self, conn):
        return [c for c in detect_curation_candidates(conn, "alice") if c["kind"] == "merge"]

    def test_shelf_level_pair_is_still_a_candidate(self):
        """棚直下どうし（表記ゆれの回収）＝統合本来の仕事は通る。"""
        conn = _make_conn()
        self._pair(conn)
        assert len(self._merges(conn)) == 1

    def test_absorbed_with_real_parent_is_not_a_candidate(self):
        """消える側が実際のページの子なら統合しない（親子・兄弟・横取りを一本で塞ぐ）。"""
        conn = _make_conn()
        _insert_page(conn, page_id="root_people", title="人物", category="people",
                     is_trunk=True, short_id=50)
        _insert_page(conn, page_id="owner", title="まはー", category="terms",
                     content="人物ページ", parent_id="root_people", short_id=51,
                     created_at=500, updated_at=500)
        self._pair(conn, b_parent="owner")
        assert self._merges(conn) == []

    def test_absorbed_with_a_child_is_not_a_candidate(self):
        """木として根を張り始めたページは吸われない。"""
        conn = _make_conn()
        self._pair(conn)
        _insert_page(conn, page_id="p_b_child", title="子ページ", category="terms",
                     content="子の中身", parent_id="p_b", short_id=3)
        assert self._merges(conn) == []

    def test_child_of_a_closed_parent_is_not_absorbed(self):
        """閉架された親にぶら下がる現役の子は吸われない（fail-closed）。

        `Memopedia.delete_page` は soft-delete で**子に波及しない**ため、閉架
        された親の下に現役の子が残る。木の形を候補一覧（未削除のみ）から引くと
        この親が見えず「親なし＝吸ってよい」に倒れていた（Codex 指摘 2026-08-05）。
        """
        conn = _make_conn()
        self._pair(conn, b_parent="closed_parent")
        # 閉架された実親（_pair が棚を作るので後から足す）
        _insert_page(conn, page_id="closed_parent", title="閉じた親", category="terms",
                     content="親の本文", parent_id="root_terms", short_id=4,
                     is_deleted=True)
        assert self._merges(conn) == [], "閉架された親を持つ現役の子が吸われた"

    def test_page_with_unresolvable_parent_is_not_absorbed(self):
        """親 id が解決できないページも吸わない（分からないものは触らない）。"""
        conn = _make_conn()
        self._pair(conn, b_parent="does_not_exist")
        assert self._merges(conn) == []

    def test_page_whose_only_child_is_closed_can_still_be_absorbed(self):
        """対照: 子が閉架だけなら根を張っていないので吸える（塞ぎすぎない）。"""
        conn = _make_conn()
        self._pair(conn)
        _insert_page(conn, page_id="dead_child", title="閉じた子", category="terms",
                     content="子の本文", parent_id="p_b", short_id=4,
                     is_deleted=True)
        assert len(self._merges(conn)) == 1

    def test_merge_that_would_be_oversized_is_not_a_candidate(self):
        """統合した結果が肥大するなら統合しない（統合が分割を呼ばない）。"""
        conn = _make_conn()
        self._pair(conn, a_content="a" * 3000, b_content="b" * 3000)
        assert self._merges(conn) == []

    def test_merge_just_under_the_line_is_still_a_candidate(self):
        """境界の反対側: 結果が閾値以下なら通る（規則が効きすぎていない）。"""
        conn = _make_conn()
        self._pair(conn, a_content="a" * 1000, b_content="b" * 1000)
        assert len(self._merges(conn)) == 1

    def test_split_pending_page_is_not_used_in_merge(self):
        """分割待ちのページに何かを足さない。"""
        conn = _make_conn()
        self._pair(conn, a_content="a" * (OVERSIZED_THRESHOLD + 10))
        assert self._merges(conn) == []

    def test_a_page_is_never_both_survivor_and_absorbed(self):
        """1 ページ 1 晩 1 操作。A←B と B←C を同じ晩に出さない。

        両方承認されると、先に閉架された B へ C の本文が流し込まれ、現役の棚に
        届かない（`get_page` は soft-delete を弾かない）。Codex 指摘 2026-08-05。
        """
        conn = _make_conn()
        _insert_page(conn, page_id="root_terms", title="用語", category="terms",
                     is_trunk=True, short_id=0)
        # A ⊃ B ⊃ C の包含関係（どの隣接ペアも類似になる）
        for pid, title, sid, created in (
            ("p_a", "SAIVerseの機能一覧について", 1, 1000),
            ("p_b", "SAIVerseの機能一覧", 2, 2000),
            ("p_c", "SAIVerseの機能", 3, 3000),
        ):
            _insert_page(conn, page_id=pid, title=title, category="terms",
                         content="x" * 100, parent_id="root_terms", short_id=sid,
                         created_at=created, updated_at=created)
        merges = self._merges(conn)
        used = [ref for m in merges for ref in m["refs"]]
        assert len(used) == len(set(used)), (
            f"同じページが複数の統合に現れた: {[m['op_id'] for m in merges]}"
        )

    def test_child_of_a_split_pending_page_is_not_used_in_merge(self):
        """分割待ちページの子も統合に使わない（操作の種類を跨いだ 1 ページ 1 操作）。

        分割は同名の既存の子へ追記するので、同じ晩にその子を残す側にした統合が
        走ると、統合の見積もり（検知時点の本文）より子が太る。
        """
        conn = _make_conn()
        _insert_page(conn, page_id="root_terms", title="用語", category="terms",
                     is_trunk=True, short_id=0)
        # 肥大した親
        _insert_page(conn, page_id="p_big", title="大きなページ", category="terms",
                     content="a" * (OVERSIZED_THRESHOLD + 10),
                     parent_id="root_terms", short_id=1)
        # その子（統合の残す側になれてしまうと衝突する）
        _insert_page(conn, page_id="p_child", title="SAIVerse機能一覧", category="terms",
                     content="b" * 100, parent_id="p_big", short_id=2,
                     created_at=1000, updated_at=1000)
        # 棚直下の相手（子を持たず実親も無い＝消える側になれる）
        _insert_page(conn, page_id="p_other", title="SAIVerse", category="terms",
                     content="c" * 100, parent_id="root_terms", short_id=3,
                     created_at=2000, updated_at=2000)

        assert self._merges(conn) == [], "分割待ちページの子が統合に使われた"

    def test_candidate_order_is_deterministic_for_same_second_pages(self):
        """同秒作成でも候補列が安定する（残す側の tie-breaker）。"""
        conn = _make_conn()
        self._pair(conn, a_title="SAIVerse機能一覧", b_title="SAIVerse")
        # 2 枚を同じ created_at に揃える
        conn.execute("UPDATE memopedia_pages SET created_at = 1000 WHERE id IN ('p_a','p_b')")
        conn.commit()
        first = [c["op_id"] for c in self._merges(conn)]
        assert first, "候補が出ていない"
        for _ in range(5):
            assert [c["op_id"] for c in self._merges(conn)] == first

    def test_one_merge_per_survivor_per_night(self):
        """同じ残す側への積み上げは一晩 1 件（候補は晩の最初に一度しか組まれない）。"""
        conn = _make_conn()
        self._pair(conn)
        # 3枚目も「SAIVerse機能一覧」に包含されるタイトル（残す側は同じ p_a）
        _insert_page(conn, page_id="p_c", title="機能一覧", category="terms",
                     content="c" * 100, parent_id="root_terms", short_id=3,
                     created_at=3000, updated_at=3000)
        merges = self._merges(conn)
        assert len(merges) == 1, f"同じ残す側の統合が複数出た: {[m['op_id'] for m in merges]}"
        assert merges[0]["op_id"].startswith("merge:memopedia:1+")


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
                "op_id": "split:memopedia:1",
                "kind": "split",
                "refs": ["memopedia:1"],
                "line": "[肥大] memopedia:1「大きいページ」 5,100字 — 子ページへの分割を提案",
            }
        ]
        schema = jp.build_day_close_schema(
            manager=manager,
            persona_id="alice",
            curation_candidates=candidates,
        )
        assert "curation_reviews" in schema["properties"]
        cr = schema["properties"]["curation_reviews"]
        # op_id の enum に候補の op_id が含まれる
        item_props = cr["items"]["properties"]
        assert "split:memopedia:1" in item_props["op_id"]["enum"]
        assert set(item_props["verdict"]["enum"]) == {"approve", "skip"}

    def test_schema_has_no_curation_reviews_when_no_candidates(self):
        from saiverse import judgment_points as jp

        manager = self._make_manager()
        schema = jp.build_day_close_schema(
            manager=manager,
            persona_id="alice",
            curation_candidates=[],
        )
        assert "curation_reviews" not in schema["properties"]

    def test_schema_has_no_curation_reviews_when_none(self):
        from saiverse import judgment_points as jp

        manager = self._make_manager()
        schema = jp.build_day_close_schema(
            manager=manager,
            persona_id="alice",
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
                "op_id": "split:memopedia:5",
                "kind": "split",
                "refs": ["memopedia:5"],
                "line": "[肥大] memopedia:5「技術の記録」 5,100字 — 子ページへの分割を提案",
            }
        ]
        output = {
            "curation_reviews": [
                {"op_id": "split:memopedia:5", "verdict": "approve"},
            ]
        }
        applied, lines, warnings = self._run_finalize(mem_conn, output, candidates)
        assert applied is True
        assert warnings == []
        pending = list_pending(mem_conn)
        assert len(pending) == 1
        assert pending[0]["op_id"] == "split:memopedia:5"
        assert pending[0]["kind"] == "split"
        # list_pending は WHERE status='pending' で絞り込み済みなので
        # status キーは返さない設計。存在を確認するには件数で十分。

    def test_skip_creates_no_plan(self, mem_conn):
        candidates = [
            {
                "op_id": "merge:memopedia:11+memopedia:12",
                "kind": "merge",
                "refs": ["memopedia:11", "memopedia:12"],
                "line": "[類似] memopedia:11「週の記録」と memopedia:12「金曜日のメモ」 — 統合を提案",
            }
        ]
        output = {
            "curation_reviews": [
                {"op_id": "merge:memopedia:11+memopedia:12", "verdict": "skip"},
            ]
        }
        applied, lines, warnings = self._run_finalize(mem_conn, output, candidates)
        assert applied is False
        assert list_pending(mem_conn) == []

    def test_duplicate_approve_creates_one_plan(self, mem_conn):
        """同じ op_id の approve を2度送っても pending 行は 1 件のまま。"""
        candidates = [
            {
                "op_id": "merge:memopedia:1+memopedia:2",
                "kind": "merge",
                "refs": ["memopedia:1", "memopedia:2"],
                "line": "[類似] ...",
            }
        ]
        output = {
            "curation_reviews": [
                {"op_id": "merge:memopedia:1+memopedia:2", "verdict": "approve"},
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
                "op_id": "split:memopedia:5",
                "kind": "split",
                "refs": ["memopedia:5"],
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
        candidates = [{"op_id": "split:memopedia:5", "kind": "split", "refs": ["memopedia:5"], "line": "..."}]
        applied, lines, warnings = self._run_finalize(mem_conn, output, candidates)
        assert applied is False


# ---------------------------------------------------------------------------
# 閾値一元化: memopedia_health が curation.py を参照
# （maintain_memopedia.py は 2026-08-05 に削除。閾値の参照元も無くなった）
# ---------------------------------------------------------------------------


class TestThresholdImports:
    def test_memopedia_health_uses_curation_thresholds(self):
        from builtin_data.tools import memopedia_health as mh_mod
        from saiverse.curation import HEALTH_LARGE_THRESHOLD, HEALTH_OVERSIZED_THRESHOLD

        assert mh_mod.HEALTH_LARGE_THRESHOLD is HEALTH_LARGE_THRESHOLD
        assert mh_mod.HEALTH_OVERSIZED_THRESHOLD is HEALTH_OVERSIZED_THRESHOLD


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
        plan_id = enqueue_plan(conn, kind="split", op_id="split:memopedia:1", refs=["memopedia:1"])
        assert isinstance(plan_id, str)
        pending = list_pending(conn)
        assert len(pending) == 1
        assert pending[0]["op_id"] == "split:memopedia:1"
        assert pending[0]["kind"] == "split"

    def test_enqueue_idempotent_on_same_op_id(self):
        conn = sqlite3.connect(":memory:", check_same_thread=False)
        init_curation_tables(conn)
        id1 = enqueue_plan(conn, kind="merge", op_id="merge:memopedia:1+memopedia:2", refs=["memopedia:1", "memopedia:2"])
        id2 = enqueue_plan(conn, kind="merge", op_id="merge:memopedia:1+memopedia:2", refs=["memopedia:1", "memopedia:2"])
        assert id1 == id2
        assert len(list_pending(conn)) == 1
