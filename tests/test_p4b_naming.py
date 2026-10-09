"""P4-b 命名（テーマ立て）の単体テスト。

メモのページ化（旧「テーマの芽」— 完了・休眠ノードのクラスタにテーマ名を
与えてページを立てる）は 2026-10-09 に休止した。材料の「目的の木」が撤去
される予定のため、検知 (``detect_naming_candidates``) と就寝判断での適用
(``_apply_naming_reviews``) は撤去し、そのテストも削除した。

残るのはページを立てる関数 ``create_theme_page`` のテストだけ。最後の呼び手
だったコマ締めの経験値ノート (旧 saiverse/slot_close.py) は v0.4 段 1-4 で
撤去され、いまは呼び手が無い (経験値ノートの新しい席は未決)。

all mocked — LLM コールなし、DB は sqlite3 インメモリ。
"""
from __future__ import annotations

import json
import sqlite3
import unittest


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _make_mem_conn() -> sqlite3.Connection:
    """インメモリ memory.db に Memopedia テーブルを用意して返す。"""
    from sai_memory.memopedia import init_memopedia_tables
    conn = sqlite3.connect(":memory:")
    init_memopedia_tables(conn)
    return conn


# ---------------------------------------------------------------------------
# create_theme_page — 新規作成・冪等
# ---------------------------------------------------------------------------

class CreateThemePageTest(unittest.TestCase):
    def setUp(self):
        self.conn = _make_mem_conn()
        from sai_memory.theme_pages import ensure_root_theme
        ensure_root_theme(self.conn)

    def test_creates_page_under_root_theme(self):
        from sai_memory.theme_pages import create_theme_page
        page_id = create_theme_page(
            self.conn,
            title="音楽の旅",
            member_refs=["task:1", "task:2", "task:3"],
        )
        self.assertIsNotNone(page_id)
        row = self.conn.execute(
            "SELECT title, parent_id FROM memopedia_pages WHERE id=?",
            (page_id,),
        ).fetchone()
        self.assertEqual(row[0], "音楽の旅")
        self.assertEqual(row[1], "root_theme")

    def test_idempotent_same_title_returns_existing_id(self):
        from sai_memory.theme_pages import create_theme_page
        id1 = create_theme_page(
            self.conn,
            title="音楽の旅",
            member_refs=["task:1", "task:2", "task:3"],
        )
        id2 = create_theme_page(
            self.conn,
            title="音楽の旅",
            member_refs=["task:1", "task:2", "task:3"],
        )
        self.assertEqual(id1, id2)
        # ページが1件だけ存在する
        count = self.conn.execute(
            "SELECT COUNT(*) FROM memopedia_pages "
            "WHERE parent_id='root_theme' AND title='音楽の旅'"
        ).fetchone()[0]
        self.assertEqual(count, 1)

    def test_metadata_contains_origin_and_member_refs(self):
        from sai_memory.theme_pages import create_theme_page
        page_id = create_theme_page(
            self.conn,
            title="料理の哲学",
            member_refs=["task:10", "task:11"],
            origin="naming",
        )
        row = self.conn.execute(
            "SELECT metadata FROM memopedia_pages WHERE id=?",
            (page_id,),
        ).fetchone()
        meta = json.loads(row[0])
        self.assertEqual(meta["origin"], "naming")
        self.assertIn("task:10", meta["member_refs"])
        self.assertIn("task:11", meta["member_refs"])

    def test_edit_history_recorded(self):
        from sai_memory.theme_pages import create_theme_page
        page_id = create_theme_page(
            self.conn,
            title="詩のひとかけら",
            member_refs=["task:5"],
        )
        row = self.conn.execute(
            "SELECT edit_source, edit_type FROM memopedia_page_edit_history "
            "WHERE page_id=?",
            (page_id,),
        ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row[0], "naming")   # edit_source
        self.assertEqual(row[1], "create")   # edit_type


class NamingPausedTest(unittest.TestCase):
    """休止の固定: 検知と就寝判断での適用の口が戻っていないこと。"""

    def test_メモのページ化の検知と適用の口が無い(self):
        from builtin_data.tools import judgment_finalize
        from saiverse import curation
        from saiverse import judgment_points as jp

        self.assertFalse(hasattr(curation, "detect_naming_candidates"))
        self.assertFalse(hasattr(curation, "NAMING_CLUSTER_MIN"))
        self.assertFalse(hasattr(judgment_finalize, "_apply_naming_reviews"))
        # 適用先だった就寝判断そのものが段 1-4 で退役した
        self.assertFalse(hasattr(jp, "build_day_close_schema"))


if __name__ == "__main__":
    unittest.main()
