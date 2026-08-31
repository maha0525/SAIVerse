"""Memopedia の「探して書く」を原子的にする口の契約 (Sol レビュー 2026-08-06)。

- F3 `apply_entity_notes`: 抽出 1 回ぶんは全部入るか何も入らないか。
  同じ出所・同じ文の Fragment は二度作らない (拾い直しの二度目)。
- F6 `upsert_page_by_title`: 探す → 書くが 1 ロック・1 トランザクション。
  ばらばらだと、隙間に入った別の書き手の追記が消えるか同名ページが二枚できる。
"""

import threading
import unittest
from unittest.mock import patch

from sai_memory.memopedia import Memopedia
from sai_memory.memopedia.core import EntityNotes
from sai_memory.memory.storage import init_db


class TestApplyEntityNotes(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = init_db(":memory:")
        self.memo = Memopedia(self.conn, db_lock=threading.RLock())

    def tearDown(self) -> None:
        self.conn.close()

    def _notes(self, page_id):
        return [f.content for f in self.memo.get_fragments(page_id)]

    def test_creates_page_and_fragments_in_one_go(self):
        results = self.memo.apply_entity_notes(
            [EntityNotes(title="エイド", parent_id="root_people",
                         summary="AI", notes=["note-1", "note-2"])],
            chronicle_entry_id="e1", source_date="2026-08-06",
        )
        self.assertEqual(len(results), 1)
        self.assertTrue(results[0].is_new_page)
        self.assertEqual(results[0].fragments_created, 2)
        self.assertEqual(self._notes(results[0].page_id), ["note-1", "note-2"])

    def test_second_apply_of_the_same_entry_is_idempotent(self):
        """⭐ 同じ出所から同じ文が二度来ても Fragment は増えない。"""
        first = self.memo.apply_entity_notes(
            [EntityNotes(title="エイド", parent_id="root_people", notes=["同じ話"])],
            chronicle_entry_id="e1",
        )
        second = self.memo.apply_entity_notes(
            [EntityNotes(title="エイド", parent_id="root_people", notes=["同じ話"])],
            chronicle_entry_id="e1",
        )
        self.assertEqual(second[0].page_id, first[0].page_id)
        self.assertFalse(second[0].is_new_page)
        self.assertEqual(second[0].fragments_created, 0)
        self.assertEqual(second[0].fragments_deduped, 1)
        self.assertEqual(self._notes(first[0].page_id), ["同じ話"])

    def test_rebuild_without_provenance_is_also_idempotent(self):
        """⭐ 出所を持たない抽出 (ログからの再構築) も二度目で増えない。

        Chronicle エントリを経由しない経路には出所 id が無い。そこで冪等判定を
        諦めると、再構築のたびに同じ知識が積み上がる。
        """
        first = self.memo.apply_entity_notes(
            [EntityNotes(title="まはー", parent_id="root_people", notes=["犬が好き"])],
        )
        second = self.memo.apply_entity_notes(
            [EntityNotes(title="まはー", parent_id="root_people", notes=["犬が好き"])],
        )
        self.assertEqual(second[0].fragments_created, 0)
        self.assertEqual(second[0].fragments_deduped, 1)
        self.assertEqual(self._notes(first[0].page_id), ["犬が好き"])

    def test_provenance_and_no_provenance_do_not_cancel_each_other(self):
        """出所ありと出所なしは別勘定 (Chronicle 由来の記録を再構築が消さない)。"""
        first = self.memo.apply_entity_notes(
            [EntityNotes(title="まはー", parent_id="root_people", notes=["犬が好き"])],
            chronicle_entry_id="e1",
        )
        self.memo.apply_entity_notes(
            [EntityNotes(title="まはー", parent_id="root_people", notes=["犬が好き"])],
        )
        self.assertEqual(self._notes(first[0].page_id), ["犬が好き", "犬が好き"])

    def test_different_entry_with_the_same_text_is_still_recorded(self):
        """出所が違えば同じ文でも記録する (別の日に同じことが分かった)。"""
        first = self.memo.apply_entity_notes(
            [EntityNotes(title="エイド", parent_id="root_people", notes=["同じ話"])],
            chronicle_entry_id="e1",
        )
        self.memo.apply_entity_notes(
            [EntityNotes(title="エイド", parent_id="root_people", notes=["同じ話"])],
            chronicle_entry_id="e2",
        )
        self.assertEqual(self._notes(first[0].page_id), ["同じ話", "同じ話"])

    def test_failure_midway_leaves_nothing(self):
        """⭐ 二人目で落ちたら、一人目のページも Fragment も残らない。"""
        with patch(
            "sai_memory.memopedia.core.storage_create_fragment",
            side_effect=[RuntimeError("disk full")],
        ):
            with self.assertRaises(RuntimeError):
                self.memo.apply_entity_notes([
                    EntityNotes(title="先", parent_id="root_people", notes=["a"]),
                    EntityNotes(title="後", parent_id="root_people", notes=["b"]),
                ], chronicle_entry_id="e1")

        self.assertIsNone(self.memo.find_by_title("先"))
        self.assertIsNone(self.memo.find_by_title("後"))

    def test_summary_is_refreshed_on_an_existing_page(self):
        page = self.memo.create_page(
            parent_id="root_people", title="まはー", summary="古い",
        )
        self.memo.apply_entity_notes(
            [EntityNotes(title="まはー", parent_id="root_people",
                         summary="新しい", notes=["note"])],
        )
        self.assertEqual(self.memo.get_page(page.id).summary, "新しい")

    def test_entity_without_notes_or_summary_is_skipped(self):
        results = self.memo.apply_entity_notes(
            [EntityNotes(title="空", parent_id="root_terms")],
        )
        self.assertEqual(results, [])
        self.assertIsNone(self.memo.find_by_title("空"))


class TestUpsertPageByTitle(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = init_db(":memory:")
        self.memo = Memopedia(self.conn, db_lock=threading.RLock())

    def tearDown(self) -> None:
        self.conn.close()

    def test_creates_when_absent(self):
        page, is_new = self.memo.upsert_page_by_title(
            title="SAIVerse", parent_id="root_terms",
            summary="プラットフォーム", append_content="本文",
        )
        self.assertTrue(is_new)
        self.assertEqual(page.content, "本文")
        self.assertEqual(page.summary, "プラットフォーム")

    def test_appends_when_present(self):
        self.memo.upsert_page_by_title(
            title="SAIVerse", parent_id="root_terms", append_content="一段目",
        )
        page, is_new = self.memo.upsert_page_by_title(
            title="SAIVerse", parent_id="root_terms", append_content="二段目",
        )
        self.assertFalse(is_new)
        self.assertEqual(page.content, "一段目\n\n二段目")
        # 同名ページが二枚できていない
        rows = self.conn.execute(
            "SELECT COUNT(*) FROM memopedia_pages WHERE title = ?", ("SAIVerse",),
        ).fetchone()[0]
        self.assertEqual(rows, 1)

    def test_summary_is_kept_when_not_given(self):
        self.memo.upsert_page_by_title(
            title="SAIVerse", parent_id="root_terms", summary="最初の要約",
            append_content="本文",
        )
        page, _ = self.memo.upsert_page_by_title(
            title="SAIVerse", parent_id="root_terms", append_content="追記",
        )
        self.assertEqual(page.summary, "最初の要約")

    def test_edit_history_records_the_given_source(self):
        page, _ = self.memo.upsert_page_by_title(
            title="SAIVerse", parent_id="root_terms", append_content="本文",
            edit_source="memopedia_generator",
        )
        sources = [h.edit_source for h in self.memo.get_page_edit_history(page.id)]
        self.assertEqual(sources, ["memopedia_generator"])


if __name__ == "__main__":
    unittest.main()
