"""知覚バッファ ストレージ層の単体テスト (sai_memory/perception_buffer.py)。

adapter を介さず sqlite conn 直で push / list / reduce / format / delete を検証する。
"""
from __future__ import annotations

import sqlite3
import unittest

from sai_memory.perception_buffer import (
    delete_perceptions,
    format_perception_message,
    init_perception_buffer_table,
    list_pending,
    push_perception,
    reduce_perceptions,
)


class PerceptionBufferTest(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        init_perception_buffer_table(self.conn)
        self.addCleanup(self.conn.close)

    def test_init_is_idempotent(self):
        # 二度呼んでも壊れない (冪等)。
        init_perception_buffer_table(self.conn)
        self.assertEqual(list_pending(self.conn), [])

    def test_push_and_list_order(self):
        push_perception(self.conn, "core_memory_correction", "A")
        push_perception(self.conn, "core_memory_correction", "B")
        items = list_pending(self.conn)
        self.assertEqual([it.content for it in items], ["A", "B"])
        self.assertEqual(items[0].kind, "core_memory_correction")
        self.assertEqual(items[0].salient, 0)
        self.assertIsNone(items[0].reduce_key)

    def test_reduce_keeps_latest_per_key(self):
        push_perception(self.conn, "core_memory_correction", "edit1", reduce_key="c:5")
        push_perception(self.conn, "core_memory_correction", "other", reduce_key="c:9")
        push_perception(self.conn, "core_memory_correction", "edit2", reduce_key="c:5")
        reduced = reduce_perceptions(list_pending(self.conn))
        contents = [it.content for it in reduced]
        # c:5 は最新 (edit2) だけ残る。c:9 はそのまま。順序は発生順を保つ。
        self.assertEqual(contents, ["other", "edit2"])

    def test_reduce_keeps_all_when_no_key(self):
        push_perception(self.conn, "k", "a")
        push_perception(self.conn, "k", "b")
        reduced = reduce_perceptions(list_pending(self.conn))
        self.assertEqual([it.content for it in reduced], ["a", "b"])

    def test_format_groups_by_kind_with_headers(self):
        push_perception(self.conn, "core_memory_correction", "訂正1")
        push_perception(self.conn, "unknown_kind", "なにか")
        text = format_perception_message(list_pending(self.conn))
        self.assertIn("[コア記憶の更新通知]", text)
        self.assertIn("訂正1", text)
        # 未知の型は汎用見出しにフォールバック。
        self.assertIn("[システム通知]", text)
        self.assertIn("なにか", text)

    def test_delete_removes_only_given_ids(self):
        i1 = push_perception(self.conn, "k", "a")
        push_perception(self.conn, "k", "b")
        delete_perceptions(self.conn, [i1])
        remaining = list_pending(self.conn)
        self.assertEqual([it.content for it in remaining], ["b"])

    def test_delete_empty_is_noop(self):
        push_perception(self.conn, "k", "a")
        delete_perceptions(self.conn, [])
        self.assertEqual(len(list_pending(self.conn)), 1)

    def test_salient_and_metadata_roundtrip(self):
        push_perception(
            self.conn, "k", "x", salient=True, metadata='{"foo": 1}', reduce_key="rk",
        )
        item = list_pending(self.conn)[0]
        self.assertEqual(item.salient, 1)
        self.assertEqual(item.metadata, '{"foo": 1}')
        self.assertEqual(item.reduce_key, "rk")


if __name__ == "__main__":
    unittest.main()
