"""カテゴリルートの trunk 播種・バックフィルの回帰テスト。

旧 `_seed_root_pages` は INITIAL_ROOTS に is_trunk を立てておらず（DEFAULT 0）、
実 DB のカテゴリルート (root_people / root_terms / root_plans / root_events) が
is_trunk=0 のまま残っていた（新しい root_core / root_theme は播種時に立てている）。
これにより trunk 除外フィルタ（編纂検知・想起・目次）がルートに効かず、特に
fold 検知が「カテゴリルートへの統合」を候補に出し得た。

検証対象:
- 新規 DB: 播種されたカテゴリルートは is_trunk=1
- 既存 DB (is_trunk=0 のルート): 再初期化で冪等にバックフィルされる。
  updated_at は変えない（編集来歴の窓集計＝新聞に「編集」として現れないため）
- 実播種の DB で、ルート直下の過小ページが fold 候補にならない
  （非 trunk の実親を持つ過小ページは従来どおり候補になる＝陽性対照）
"""
from __future__ import annotations

import sqlite3
import time

from sai_memory.memopedia.storage import (
    INITIAL_ROOTS,
    create_page,
    init_memopedia_tables,
)
from saiverse.curation import (
    UNDERSIZED_THRESHOLD,
    detect_curation_candidates,
)


def _fresh_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    init_memopedia_tables(conn)
    return conn


def _root_flags(conn: sqlite3.Connection) -> dict:
    root_ids = [r["id"] for r in INITIAL_ROOTS]
    placeholders = ",".join("?" for _ in root_ids)
    rows = conn.execute(
        f"SELECT id, is_trunk, updated_at FROM memopedia_pages WHERE id IN ({placeholders})",
        root_ids,
    ).fetchall()
    return {row[0]: {"is_trunk": row[1], "updated_at": row[2]} for row in rows}


class TestRootTrunkSeeding:
    def test_new_db_seeds_roots_as_trunk(self):
        """新規 DB の播種でカテゴリルート全部に is_trunk=1 が立つ。"""
        conn = _fresh_conn()
        flags = _root_flags(conn)
        assert len(flags) == len(INITIAL_ROOTS)
        for root_id, row in flags.items():
            assert row["is_trunk"] == 1, f"{root_id} が trunk になっていない"

    def test_legacy_db_roots_backfilled_idempotently(self):
        """旧 seed 由来の is_trunk=0 ルートが再初期化で trunk にバックフィルされる。

        updated_at は変えない（編集来歴の窓集計に現れてはいけない）。
        """
        conn = _fresh_conn()
        # 旧 seed の状態を再現: ルートの is_trunk を 0 に落とす
        conn.execute("UPDATE memopedia_pages SET is_trunk = 0 WHERE parent_id IS NULL")
        conn.commit()
        before = _root_flags(conn)
        assert all(v["is_trunk"] == 0 for v in before.values())

        init_memopedia_tables(conn)  # 起動時の冪等初期化を再実行

        after = _root_flags(conn)
        for root_id, row in after.items():
            assert row["is_trunk"] == 1, f"{root_id} がバックフィルされていない"
            assert row["updated_at"] == before[root_id]["updated_at"], (
                f"{root_id} の updated_at が変わった（窓集計に編集として現れてしまう）"
            )


class TestRootDirectChildNotFoldCandidate:
    """実播種の DB で「trunk 直下は fold 候補にしない」ガードが効くこと。

    旧 seed ではルートが is_trunk=0 のため _has_real_parent がルートを実親と
    誤認し、ルートへの fold（カテゴリコンテナへの本文統合）が候補に出ていた。
    """

    def _make_stale_undersized(
        self, conn: sqlite3.Connection, *, parent_id: str, title: str
    ) -> str:
        page = create_page(
            conn,
            parent_id=parent_id,
            title=title,
            content="小さなメモ。",
            category="people",
        )
        assert len(page.content) < UNDERSIZED_THRESHOLD
        old = int(time.time()) - 90 * 86400
        conn.execute(
            "UPDATE memopedia_pages SET updated_at = ?, last_referenced_at = ? WHERE id = ?",
            (old, old, page.id),
        )
        conn.commit()
        return page.id

    def test_page_directly_under_seeded_root_is_not_fold_candidate(self):
        conn = _fresh_conn()
        self._make_stale_undersized(conn, parent_id="root_people", title="古い知人")
        candidates = detect_curation_candidates(conn, "test_persona")
        folds = [c for c in candidates if c["kind"] == "fold"]
        assert folds == [], (
            f"ルート直下の過小ページが fold 候補になった（ルートへの統合提案）: {folds}"
        )

    def test_page_under_real_parent_is_fold_candidate(self):
        """陽性対照: 非 trunk の実親を持つ過小ページは従来どおり候補になる。"""
        conn = _fresh_conn()
        parent = create_page(
            conn,
            parent_id="root_people",
            title="旧友たち",
            content="学生時代からの友人についての記録。" * 10,
            category="people",
        )
        self._make_stale_undersized(conn, parent_id=parent.id, title="古い知人")
        candidates = detect_curation_candidates(conn, "test_persona")
        folds = [c for c in candidates if c["kind"] == "fold"]
        assert len(folds) == 1, f"実親持ちの過小ページが fold 候補にならない: {candidates}"
        assert len(folds[0]["refs"]) == 2
