"""コア記憶ストレージ (sai_memory/core_memory.py) の CRUD テスト。"""
import sqlite3
import unittest

from sai_memory.core_memory import (
    add_core_memory,
    confirm_core_memory,
    count_unconfirmed_core_memories,
    init_core_memory_table,
    list_core_memories,
    list_deleted_core_memories,
    remove_core_memory,
    restore_core_memory,
    total_core_memory_chars,
    update_core_memory,
)


class CoreMemoryStorageTest(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        init_core_memory_table(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_init_creates_table_with_all_columns(self):
        cols = {row[1] for row in self.conn.execute("PRAGMA table_info(core_memories)")}
        self.assertEqual(
            cols,
            {"id", "content", "created_at", "updated_at", "kind", "metadata",
             "confirmed", "deleted_at"},
        )

    def test_init_idempotent(self):
        init_core_memory_table(self.conn)  # 2 回目でも壊れない
        self.assertEqual(list_core_memories(self.conn), [])

    def test_add_returns_id_and_lists_ordered(self):
        id1 = add_core_memory(self.conn, "まはーの誕生日は1月14日")
        id2 = add_core_memory(self.conn, "私は「エア」という名前")
        self.assertEqual(id1, 1)
        self.assertEqual(id2, 2)
        items = list_core_memories(self.conn)
        self.assertEqual([i.id for i in items], [1, 2])
        self.assertEqual(items[0].content, "まはーの誕生日は1月14日")
        # 既定 kind は 'note'、metadata は None。
        self.assertEqual(items[0].kind, "note")
        self.assertIsNone(items[0].metadata)
        self.assertEqual(items[0].ref, "c:1")

    def test_update_existing(self):
        mid = add_core_memory(self.conn, "旧い内容")
        ok = update_core_memory(self.conn, mid, "新しい内容")
        self.assertTrue(ok)
        items = list_core_memories(self.conn)
        self.assertEqual(items[0].content, "新しい内容")

    def test_update_missing_returns_false(self):
        self.assertFalse(update_core_memory(self.conn, 999, "x"))

    def test_remove_existing(self):
        mid = add_core_memory(self.conn, "消す対象")
        self.assertTrue(remove_core_memory(self.conn, mid))
        self.assertEqual(list_core_memories(self.conn), [])

    def test_remove_missing_returns_false(self):
        self.assertFalse(remove_core_memory(self.conn, 999))

    def test_total_chars(self):
        self.assertEqual(total_core_memory_chars(self.conn), 0)
        add_core_memory(self.conn, "12345")   # 5 字
        add_core_memory(self.conn, "あいう")  # 3 字
        self.assertEqual(total_core_memory_chars(self.conn), 8)

    # -- confirmed (未確認フラグ) --------------------------------------

    def test_add_defaults_to_confirmed(self):
        add_core_memory(self.conn, "手動追加は確認済み")
        self.assertEqual(count_unconfirmed_core_memories(self.conn), 0)
        self.assertEqual(list_core_memories(self.conn)[0].confirmed, 1)

    def test_add_unconfirmed_counts_and_confirm_clears(self):
        mid = add_core_memory(self.conn, "自動採取", confirmed=0)
        self.assertEqual(count_unconfirmed_core_memories(self.conn), 1)
        self.assertEqual(list_core_memories(self.conn)[0].confirmed, 0)
        self.assertTrue(confirm_core_memory(self.conn, mid))
        self.assertEqual(count_unconfirmed_core_memories(self.conn), 0)

    def test_update_can_reset_confirmed(self):
        mid = add_core_memory(self.conn, "確認済み")  # confirmed=1
        update_core_memory(self.conn, mid, "書き換え", confirmed=0)
        self.assertEqual(list_core_memories(self.conn)[0].confirmed, 0)

    # -- soft-delete (ごみ箱) + 復元 -----------------------------------

    def test_remove_is_soft_delete_and_listed_in_trash(self):
        mid = add_core_memory(self.conn, "削除対象")
        self.assertTrue(remove_core_memory(self.conn, mid))
        # 生存一覧からは消えるが、ごみ箱に残り物理削除されていない。
        self.assertEqual(list_core_memories(self.conn), [])
        trash = list_deleted_core_memories(self.conn)
        self.assertEqual([i.id for i in trash], [mid])
        self.assertIsNotNone(trash[0].deleted_at)

    def test_restore_from_trash(self):
        mid = add_core_memory(self.conn, "復元対象")
        remove_core_memory(self.conn, mid)
        self.assertTrue(restore_core_memory(self.conn, mid))
        self.assertEqual([i.id for i in list_core_memories(self.conn)], [mid])
        self.assertEqual(list_deleted_core_memories(self.conn), [])

    def test_double_remove_returns_false(self):
        mid = add_core_memory(self.conn, "x")
        self.assertTrue(remove_core_memory(self.conn, mid))
        self.assertFalse(remove_core_memory(self.conn, mid))  # 二重削除しない

    def test_total_chars_excludes_deleted(self):
        add_core_memory(self.conn, "12345")   # 5 字
        mid = add_core_memory(self.conn, "あいう")  # 3 字
        remove_core_memory(self.conn, mid)
        self.assertEqual(total_core_memory_chars(self.conn), 5)


if __name__ == "__main__":
    unittest.main()
