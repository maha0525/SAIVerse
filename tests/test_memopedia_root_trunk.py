"""カテゴリルートの trunk 播種・バックフィルの回帰テスト。

旧 `_seed_root_pages` は INITIAL_ROOTS に is_trunk を立てておらず（DEFAULT 0）、
実 DB のカテゴリルート (root_people / root_terms / root_plans / root_events) が
is_trunk=0 のまま残っていた（新しい root_core / root_theme は播種時に立てている）。
これにより trunk 除外フィルタ（編纂検知・想起・目次）がルートに効かず、当時は
fold 検知が「カテゴリルートへの統合」を候補に出し得た（fold は 2026-08-05 に撤去）。

検証対象:
- 新規 DB: 播種されたカテゴリルートは is_trunk=1
- 既存 DB (is_trunk=0 のルート): 再初期化で冪等にバックフィルされる。
  updated_at は変えない（編集来歴の窓集計＝新聞に「編集」として現れないため）
- 実播種の DB で、ルートが編纂から「棚」として扱われる（棚直下は統合できる /
  実ページの子は統合の消える側になれない / 棚そのものは分割候補にならない）
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
    OVERSIZED_THRESHOLD,
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


class TestRootIsTrunkForCuration:
    """実播種の DB で、カテゴリルートが編纂から「棚」として扱われること。

    旧 seed ではルートが is_trunk=0 のため `_has_real_parent` がルートを実親と
    誤認していた。当時この誤りは fold（ルートへの本文統合）として現れたが、
    fold は 2026-08-05 に撤去済み。同じ性質は現在「消える側になれるのは実際の
    ページを親にも子にも持たないページだけ」という統合の規則に現れる——棚の
    直下に並ぶページは統合できる／実ページの子は統合できない、の差で観測する。
    """

    def _make_pair(self, conn: sqlite3.Connection, *, second_parent: str) -> None:
        """タイトル包含で類似になる 2 枚（古い方＝ルート直下が残る側）。"""
        old = int(time.time()) - 90 * 86400
        keep = create_page(
            conn, parent_id="root_people", title="友人たちの記録",
            content="学生時代からの友人についての記録。", category="people",
        )
        conn.execute(
            "UPDATE memopedia_pages SET created_at = ? WHERE id = ?", (old, keep.id)
        )
        create_page(
            conn, parent_id=second_parent, title="友人たち",
            content="小さなメモ。", category="people",
        )
        conn.commit()

    def test_page_directly_under_seeded_root_can_be_absorbed(self):
        """棚直下どうし＝表記ゆれの回収（統合本来の仕事）は通る。"""
        conn = _fresh_conn()
        self._make_pair(conn, second_parent="root_people")
        merges = [
            c for c in detect_curation_candidates(conn, "test_persona")
            if c["kind"] == "merge"
        ]
        assert len(merges) == 1, (
            f"ルートが棚として扱われていない（統合候補が出ない）: {merges}"
        )

    def test_page_under_a_real_parent_cannot_be_absorbed(self):
        """対照: 実ページの子は統合の消える側になれない。"""
        conn = _fresh_conn()
        owner = create_page(
            conn, parent_id="root_people", title="旧友たち",
            content="学生時代からの友人についての記録。" * 10, category="people",
        )
        self._make_pair(conn, second_parent=owner.id)
        merges = [
            c for c in detect_curation_candidates(conn, "test_persona")
            if c["kind"] == "merge"
        ]
        assert merges == [], f"実親を持つページが吸われる候補になった: {merges}"

    def test_seeded_root_is_never_a_split_candidate(self):
        """棚そのものは肥大しても分割候補にしない（trunk 除外）。"""
        conn = _fresh_conn()
        conn.execute(
            "UPDATE memopedia_pages SET content = ? WHERE id = 'root_people'",
            ("a" * (OVERSIZED_THRESHOLD + 100),),
        )
        conn.commit()
        candidates = detect_curation_candidates(conn, "test_persona")
        assert candidates == [], f"カテゴリルートが編纂候補になった: {candidates}"
