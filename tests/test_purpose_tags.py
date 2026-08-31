"""purpose_tags ストア sai_memory/purpose_tags.py のテスト (life_concept_map.md §9.1)。

検証項目:
- init の冪等性 / add・get の基本 CRUD と layer 検証
- 同一 (target_ref, purpose_ref) の upsert 一意性 (行は増えず layer 最新・
  revisit_count+1・created_at は初回のまま)
- list_by_purpose / list_by_target の 2 方向クエリ
- touch_revisit の刻印 (層は変えない)
- created_at が saiverse.clock 経由 (仮想クロックの epoch がそのまま刻まれる)
"""
from __future__ import annotations

import gc
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sai_memory import purpose_tags as PT
from saiverse import clock


class PurposeTagsTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        # 実運用ではペルソナの memory.db に相乗りする (module docstring 参照)
        self.db_path = str(Path(self._tmpdir.name) / "memory.db")
        self.conn = sqlite3.connect(self.db_path)
        PT.init_purpose_tags_tables(self.conn)

    def tearDown(self):
        clock.disable_virtual()
        self.conn.close()
        gc.collect()
        try:
            self._tmpdir.cleanup()
        except PermissionError:
            pass

    def _count(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM purpose_tags").fetchone()[0]

    # ---- init / CRUD ----

    def test_init_is_idempotent(self):
        PT.init_purpose_tags_tables(self.conn)  # 2 回目も例外なし
        PT.add_tag(
            self.conn, target_ref="message:m1", purpose_ref="task:1",
            layer=PT.LAYER_SHELVE,
        )
        PT.init_purpose_tags_tables(self.conn)  # 既存データを壊さない
        self.assertEqual(self._count(), 1)

    def test_add_and_get(self):
        tag = PT.add_tag(
            self.conn, target_ref="episode:3", purpose_ref="track:1",
            layer=PT.LAYER_SHELVE,
        )
        got = PT.get_tag(self.conn, tag.tag_id)
        self.assertEqual(got, tag)
        self.assertEqual(got.layer, PT.LAYER_SHELVE)
        self.assertEqual(got.revisit_count, 0)

    def test_add_requires_refs_and_valid_layer(self):
        with self.assertRaises(ValueError):
            PT.add_tag(self.conn, target_ref="", purpose_ref="task:1", layer=2)
        with self.assertRaises(ValueError):
            PT.add_tag(self.conn, target_ref="message:m1", purpose_ref="", layer=2)
        # 層 0 (継承) は導出・層 1 は mark なので保存しない (§9.1)
        for bad in (0, 1, 5):
            with self.assertRaises(ValueError):
                PT.add_tag(
                    self.conn, target_ref="message:m1", purpose_ref="task:1",
                    layer=bad,
                )

    def test_created_at_follows_virtual_clock(self):
        t = datetime(2026, 7, 6, 14, 0, 0)
        clock.enable_virtual(t)
        tag = PT.add_tag(
            self.conn, target_ref="message:m1", purpose_ref="task:1",
            layer=PT.LAYER_REVISIT,
        )
        self.assertEqual(tag.created_at, int(t.timestamp()))

    # ---- upsert 一意性 ----

    def test_same_pair_upserts_instead_of_duplicating(self):
        clock.enable_virtual(datetime(2026, 7, 6, 9, 0, 0))
        first = PT.add_tag(
            self.conn, target_ref="message:m1", purpose_ref="task:1",
            layer=PT.LAYER_SHELVE,
        )
        clock.advance_to(datetime(2026, 7, 6, 18, 0, 0))
        second = PT.add_tag(
            self.conn, target_ref="message:m1", purpose_ref="task:1",
            layer=PT.LAYER_METABOLISM,
        )
        # 行は増えず、同じ行に濃さが積まれる
        self.assertEqual(self._count(), 1)
        self.assertEqual(second.tag_id, first.tag_id)
        self.assertEqual(second.layer, PT.LAYER_METABOLISM)  # layer 最新
        self.assertEqual(second.revisit_count, 1)            # revisit_count+1
        self.assertEqual(second.created_at, first.created_at)  # 初回の来歴を保持

    def test_different_pairs_are_separate_rows(self):
        PT.add_tag(self.conn, target_ref="message:m1", purpose_ref="task:1", layer=2)
        PT.add_tag(self.conn, target_ref="message:m1", purpose_ref="task:2", layer=2)
        PT.add_tag(self.conn, target_ref="message:m2", purpose_ref="task:1", layer=2)
        self.assertEqual(self._count(), 3)

    # ---- 2 方向クエリ ----

    def test_list_by_purpose_and_by_target(self):
        clock.enable_virtual(datetime(2026, 7, 6, 9, 0, 0))
        old = PT.add_tag(
            self.conn, target_ref="message:m1", purpose_ref="task:1", layer=2,
        )
        clock.advance_to(datetime(2026, 7, 6, 12, 0, 0))
        new = PT.add_tag(
            self.conn, target_ref="episode:2", purpose_ref="task:1", layer=2,
        )
        PT.add_tag(self.conn, target_ref="message:m1", purpose_ref="track:9", layer=3)

        by_purpose = PT.list_by_purpose(self.conn, "task:1")
        # 新しさ降順
        self.assertEqual([t.tag_id for t in by_purpose], [new.tag_id, old.tag_id])

        by_target = PT.list_by_target(self.conn, "message:m1")
        self.assertEqual(
            sorted(t.purpose_ref for t in by_target), ["task:1", "track:9"]
        )

        self.assertEqual(PT.list_by_purpose(self.conn, "task:999"), [])

    # ---- touch_revisit ----

    def test_touch_revisit_increments_without_layer_change(self):
        tag = PT.add_tag(
            self.conn, target_ref="message:m1", purpose_ref="task:1",
            layer=PT.LAYER_SHELVE,
        )
        touched = PT.touch_revisit(self.conn, tag.tag_id)
        self.assertIsNotNone(touched)
        self.assertEqual(touched.revisit_count, 1)
        self.assertEqual(touched.layer, PT.LAYER_SHELVE)  # 層は変えない
        touched = PT.touch_revisit(self.conn, tag.tag_id)
        self.assertEqual(touched.revisit_count, 2)

    def test_touch_revisit_unknown_id_returns_none(self):
        self.assertIsNone(PT.touch_revisit(self.conn, "no-such-tag"))


if __name__ == "__main__":
    unittest.main()
